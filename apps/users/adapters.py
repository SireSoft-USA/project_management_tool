from django.utils.translation import gettext_lazy as _

from allauth.account import app_settings
from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.utils import user_email, user_field

from apps.notifications.emails import build_logo_url


class UserEmailAsUsernameAdapter(DefaultAccountAdapter):
    def __init__(self, request=None):
        super().__init__(request)
        self.error_messages["email_taken"] = _("Coldn't create your account.")

    def populate_username(self, request, user):
        user_field(user, app_settings.USER_MODEL_USERNAME_FIELD, user_email(user))

    def send_mail(self, template_prefix, email, context):
        """Add logo_url so allauth's own emails (confirmation, password reset)
        share the same branded header as notification emails — see
        build_logo_url() for why this is a static() call, not a database read."""
        context.setdefault("logo_url", build_logo_url())
        super().send_mail(template_prefix, email, context)
