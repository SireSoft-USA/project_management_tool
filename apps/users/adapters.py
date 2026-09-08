from django.utils.translation import gettext_lazy as _

from allauth.account import app_settings
from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.utils import user_email, user_field

from apps.notifications.emails import LOGO_CID, LogoEmailMessage, build_logo_url, logo_bytes


class UserEmailAsUsernameAdapter(DefaultAccountAdapter):
    def __init__(self, request=None):
        super().__init__(request)
        self.error_messages["email_taken"] = _("Coldn't create your account.")

    def populate_username(self, request, user):
        user_field(user, app_settings.USER_MODEL_USERNAME_FIELD, user_email(user))

    def send_mail(self, template_prefix, email, context):
        """Give allauth's own emails (confirmation, password reset) the same
        branded header as notification emails.

        logo_cid selects the embedded copy attached in render_mail(); logo_url is
        the fallback used when embedding failed.
        """
        context.setdefault("logo_cid", LOGO_CID if logo_bytes() else "")
        context.setdefault("logo_url", build_logo_url())
        super().send_mail(template_prefix, email, context)

    def render_mail(self, template_prefix, email, context, headers=None):
        """Embed the logo in the message allauth built.

        allauth constructs the EmailMultiAlternatives itself, so the image is
        moved onto a LogoEmailMessage here — the subclass is what produces the
        multipart/related structure a cid: reference needs.
        """
        message = super().render_mail(template_prefix, email, context, headers=headers)
        logo = logo_bytes()
        if not logo or not getattr(message, "alternatives", None):
            # Plain-text mail has no HTML part to hold an inline image.
            return message

        related = LogoEmailMessage(
            subject=message.subject,
            body=message.body,
            from_email=message.from_email,
            to=message.to,
            cc=message.cc,
            bcc=message.bcc,
            reply_to=message.reply_to,
            headers=message.extra_headers,
            connection=message.connection,
            logo=logo,
        )
        related.alternatives = message.alternatives
        return related
