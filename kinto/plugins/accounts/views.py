import colander
import re
import string
import uuid
from pyramid import httpexceptions
from pyramid.decorator import reify
from pyramid.security import Authenticated, Everyone
from pyramid.settings import aslist
from pyramid.events import subscriber
from pyramid_mailer import get_mailer
from pyramid_mailer.message import Message


from kinto.views import NameGenerator
from kinto.core import Service, resource, utils
from kinto.core.errors import raise_invalid, http_error
from kinto.core.events import ResourceChanged, ACTIONS
from kinto.core.storage import exceptions as storage_exceptions

from .utils import (
    hash_password,
    is_validated,
    ACCOUNT_CACHE_KEY,
    ACCOUNT_POLICY_NAME,
    ACCOUNT_RESET_PASSWORD_CACHE_KEY,
    ACCOUNT_VALIDATION_CACHE_KEY,
)


def _extract_posted_body_id(request):
    try:
        # Anonymous creation with POST.
        return request.json["data"]["id"]
    except (ValueError, KeyError):
        # Bad POST data.
        if request.method.lower() == "post":
            error_details = {"name": "data.id", "description": "data.id in body: Required"}
            raise_invalid(request, **error_details)
        # Anonymous GET
        error_msg = "Cannot read accounts."
        raise http_error(httpexceptions.HTTPUnauthorized(), error=error_msg)


class AccountIdGenerator(NameGenerator):
    """Allow @ signs in account IDs."""

    regexp = r"^[a-zA-Z0-9][.@a-zA-Z0-9_-]*$"


class AccountSchema(resource.ResourceSchema):
    password = colander.SchemaNode(colander.String())


class MyFormatter(string.Formatter):
    """Formatter class that will not fail if there's a missing key."""

    def __init__(self, default="{{{0}}}"):
        self.default = default

    def get_value(self, key, args, kwargs):
        return kwargs.get(key, self.default.format(key))


@resource.register()
class Account(resource.Resource):

    schema = AccountSchema

    def __init__(self, request, context):
        settings = request.registry.settings
        # Store if current user is administrator (before accessing get_parent_id())
        allowed_from_settings = settings.get("account_write_principals", [])
        context.is_administrator = (
            len(set(aslist(allowed_from_settings)) & set(request.prefixed_principals)) > 0
        )
        # Shortcut to check if current is anonymous (before get_parent_id()).
        context.is_anonymous = Authenticated not in request.effective_principals
        # Is the "accounts validation" setting set?
        context.validation = settings.get("account_validation", False)
        # Account validation requires the user id to be an email.
        validation_email_regexp = settings.get(
            "account_validation.email_regexp", "^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z0-9-.]+$"
        )
        context.validation_email_regexp = re.compile(validation_email_regexp)

        super().__init__(request, context)

        # Overwrite the current principal set by Resource.
        if self.model.current_principal == Everyone or context.is_administrator:
            # Creation is anonymous, but author with write perm is this:
            self.model.current_principal = f"{ACCOUNT_POLICY_NAME}:{self.model.parent_id}"

        if context.validation:
            # pyramid_mailer instance.
            self.mailer = get_mailer(request)
            self.email_sender = settings.get(
                "account_validation.email_sender", "admin@example.com"
            )
            self.email_subject_template = settings.get(
                "account_validation.email_subject_template", "activate your account"
            )
            self.email_body_template = settings.get(
                "account_validation.email_body_template", "{activation-key}"
            )

    @reify
    def id_generator(self):
        # This generator is used for ID validation.
        return AccountIdGenerator()

    def get_parent_id(self, request):
        # The whole challenge here is that we want to isolate what
        # authenticated users can list, but give access to everything to
        # administrators.
        # Plus when anonymous create accounts, we have to set their parent id
        # to the same value they would obtain when authenticated.
        if self.context.is_administrator:
            if self.context.on_plural_endpoint:
                # Accounts created by admin should have userid as parent.
                if request.method.lower() == "post":
                    return _extract_posted_body_id(request)
                else:
                    # Admin see all accounts.
                    return "*"
            else:
                # No pattern matching for admin on single record.
                return request.matchdict["id"]

        if not self.context.is_anonymous:
            # Authenticated users see their own account only.
            return request.selected_userid

        # Anonymous creation with PUT.
        if "id" in request.matchdict:
            return request.matchdict["id"]

        return _extract_posted_body_id(request)

    def plural_post(self):
        result = super(Account, self).plural_post()
        if self.context.is_anonymous and self.request.response.status_code == 200:
            error_details = {"message": "Account ID %r already exists" % result["data"]["id"]}
            raise http_error(httpexceptions.HTTPForbidden(), **error_details)
        return result

    def process_object(self, new, old=None):
        new = super(Account, self).process_object(new, old)

        new["password"] = hash_password(new["password"])

        # Do not let accounts be created without usernames.
        if self.model.id_field not in new:
            error_details = {"name": "data.id", "description": "Accounts must have an ID."}
            raise_invalid(self.request, **error_details)

        # Account validation requires that the record ID is an email address.
        # TODO: this might be better suited for a schema. Do we have a way to
        # dynamically change the schema according to the settings?
        if self.context.validation and old is None:
            email_regexp = self.context.validation_email_regexp
            # Account validation requires that the record ID is an email address.
            user_email = new[self.model.id_field]
            if not email_regexp.match(user_email):
                error_details = {
                    "name": "data.id",
                    "description": f"Account validation is enabled, and user id should match {email_regexp}",
                }
                raise_invalid(self.request, **error_details)

            activation_key = str(uuid.uuid4())
            extra_data = {"activation-key": activation_key}
            new["validated"] = False

            # Store the activation key in the cache to be used in the `validate` endpoint.
            cache_validation_key(activation_key, new["id"], self.request.registry)

            # Send an email to the user with the link to activate their account.
            email_context = new.get("email-context", {})
            formatter = MyFormatter()

            formatted_subject = formatter.format(
                self.email_subject_template, **new, **extra_data, **email_context
            )
            formatted_body = formatter.format(
                self.email_body_template, **new, **extra_data, **email_context
            )

            message = Message(
                subject=formatted_subject,
                sender=self.email_sender,
                recipients=[user_email],
                body=formatted_body,
            )
            self.mailer.send(message)

        # Administrators can reach other accounts and anonymous have no
        # selected_userid. So do not try to enforce.
        if self.context.is_administrator or self.context.is_anonymous:
            return new

        # Otherwise, we force the id to match the authenticated username.
        if new[self.model.id_field] != self.request.selected_userid:
            error_details = {
                "name": "data.id",
                "description": "Username and account ID do not match.",
            }
            raise_invalid(self.request, **error_details)

        return new


# Clear cache on account change
@subscriber(
    ResourceChanged, for_resources=("account",), for_actions=(ACTIONS.UPDATE, ACTIONS.DELETE)
)
def on_account_changed(event):
    request = event.request
    cache = request.registry.cache
    settings = request.registry.settings
    # Extract username and password from current user
    username = request.matchdict["id"]
    hmac_secret = settings["userid_hmac_secret"]
    cache_key = utils.hmac_digest(hmac_secret, ACCOUNT_CACHE_KEY.format(username))
    # Delete cache
    cache.delete(cache_key)


# Account validation (enable in the settings).
validation = Service(
    name="account-validation",
    path="/accounts/{user_id}/validate/{activation_key}",
    description="Validate an account",
)


def cache_validation_key(activation_key, username, registry):
    """Store a validation_key in the cache."""
    settings = registry.settings
    hmac_secret = settings["userid_hmac_secret"]
    cache_key = utils.hmac_digest(hmac_secret, ACCOUNT_VALIDATION_CACHE_KEY.format(username))
    # Store an activation key for 7 days by default.
    cache_ttl = int(
        settings.get("account_validation.validation_key_cache_ttl_seconds", 7 * 24 * 60 * 60)
    )

    cache = registry.cache
    cache.set(cache_key, activation_key, ttl=cache_ttl)


def check_validation_key(activation_key, username, registry):
    """Given a username, compare the activation-key provided with the one from the cache."""
    hmac_secret = registry.settings["userid_hmac_secret"]
    cache_key = utils.hmac_digest(hmac_secret, ACCOUNT_VALIDATION_CACHE_KEY.format(username))

    cache = registry.cache
    cache_result = cache.get(cache_key)

    if cache_result == activation_key:
        cache.delete(cache_key)  # We're done with the activation key.
        return True
    return False


@validation.post()
def post_validation(request):
    user_id = request.matchdict["user_id"]
    activation_key = request.matchdict["activation_key"]

    parent_id = user_id
    try:
        user = request.registry.storage.get(
            parent_id=parent_id, resource_name="account", object_id=user_id
        )
    except storage_exceptions.ObjectNotFoundError:
        # Don't give information on the existence of a user id: return a generic error message.
        error_details = {"message": "Account ID and activation key do not match"}
        raise http_error(httpexceptions.HTTPForbidden(), **error_details)

    if is_validated(user):
        error_details = {"message": f"Account {user_id} has already been validated"}
        raise http_error(httpexceptions.HTTPForbidden(), **error_details)

    if not check_validation_key(activation_key, user_id, request.registry):
        error_details = {"message": "Account ID and activation key do not match"}
        raise http_error(httpexceptions.HTTPForbidden(), **error_details)

    # User is now validated.
    user["validated"] = True

    request.registry.storage.update(
        parent_id=parent_id, resource_name="account", object_id=user_id, record=user
    )

    # Send a confirmation email.
    settings = request.registry.settings
    email_confirmation_subject_template = settings.get(
        "account_validation.email_confirmation_subject_template", "Account active"
    )
    email_confirmation_body_template = settings.get(
        "account_validation.email_confirmation_body_template", "The account {id} is now active"
    )
    email_sender = settings.get("account_validation.email_sender", "admin@example.com")

    user_email = user["id"]
    mailer = get_mailer(request)
    email_context = user.get("email-context", {})

    formatter = MyFormatter()
    formatted_subject = formatter.format(
        email_confirmation_subject_template, **user, **email_context
    )
    formatted_body = formatter.format(email_confirmation_body_template, **user, **email_context)

    message = Message(
        subject=formatted_subject,
        sender=email_sender,
        recipients=[user_email],
        body=formatted_body,
    )
    mailer.send(message)

    return user


# Password reset.
reset_password = Service(
    name="reset-password",
    path="/accounts/{user_id}/reset-password",
    description="Send a temporary reset password by mail for an account",
)


def cache_reset_password(reset_password, username, registry):
    """Store a reset-password in the cache."""
    settings = registry.settings
    hmac_secret = settings["userid_hmac_secret"]
    cache_key = utils.hmac_digest(hmac_secret, ACCOUNT_RESET_PASSWORD_CACHE_KEY.format(username))
    # Store a reset password for 7 days by default.
    cache_ttl = int(
        settings.get("account_validation.reset_password_cache_ttl_seconds", 7 * 24 * 60 * 60)
    )

    cache = registry.cache
    cache.set(cache_key, reset_password, ttl=cache_ttl)


@reset_password.post()
def post_reset_password(request):
    user_id = request.matchdict["user_id"]

    parent_id = user_id
    try:
        user = request.registry.storage.get(
            parent_id=parent_id, resource_name="account", object_id=user_id
        )
    except storage_exceptions.ObjectNotFoundError:
        # Don't give information on the existence of a user id: return a generic message.
        return {"message": "A temporary reset password has been sent by mail"}

    settings = request.registry.settings

    user_email = user["id"]
    email_regexp = settings.get(
        "account_validation.email_regexp", "^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z0-9-.]+$"
    )
    compiled_email_regexp = re.compile(email_regexp)
    if not compiled_email_regexp.match(user_email):
        error_details = {
            "name": "data.id",
            "description": f"The user id should match {email_regexp}.",
        }
        raise_invalid(request, **error_details)

    reset_password = str(uuid.uuid4())
    extra_data = {"reset-password": reset_password}
    hashed_reset_password = hash_password(reset_password)
    cache_reset_password(hashed_reset_password, user_id, request.registry)

    # Send a temporary reset password by mail.
    email_reset_password_subject_template = settings.get(
        "account_validation.email_reset_password_subject_template", "Reset password"
    )
    email_reset_password_body_template = settings.get(
        "account_validation.email_reset_password_body_template", "{reset-password}"
    )
    email_sender = settings.get("account_validation.email_sender", "admin@example.com")

    mailer = get_mailer(request)
    user_email_context = user.get(
        "email-context", {}
    )  # We might have some previous email context.
    data = request.json.get("data", {})
    email_context = data.get("email-context", user_email_context)

    formatter = MyFormatter()
    formatted_subject = formatter.format(
        email_reset_password_subject_template, **user, **extra_data, **email_context
    )
    formatted_body = formatter.format(
        email_reset_password_body_template, **user, **extra_data, **email_context
    )

    message = Message(
        subject=formatted_subject,
        sender=email_sender,
        recipients=[user_email],
        body=formatted_body,
    )
    mailer.send(message)

    return {"message": "A temporary reset password has been sent by mail"}
