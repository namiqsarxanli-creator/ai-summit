# ruff: noqa: E501  (the email copy below is prose)
"""The OTP email in Azerbaijani, English and Russian.

Deliverability rules from the original adapter are kept: the code leads the
subject (the pattern mailbox providers expect from OTP mail), a plain-text
part accompanies the HTML, and Reply-To is a monitored mailbox (set by the
sender, not here).
"""

from __future__ import annotations

import html as html_lib
from dataclasses import dataclass

from app.core.languages import DEFAULT_LANGUAGE, Language
from app.modules.attendees.models import OtpPurpose

# Kept equal to attendees.service.OTP_EXPIRY_MINUTES; the strings below
# spell out "10" in each language's grammar, and a test pins the two together.
EXPIRY_MINUTES = 10


@dataclass(frozen=True)
class _Copy:
    subject_activation: str  # {code} is substituted
    subject_login: str
    # A STAFF member's password-reset code: its own wording, which says what the
    # code is for and that ignoring the email leaves the password as it was.
    subject_reset: str
    greeting_reset: str
    ignore_reset: str
    greeting: str  # "Your AI Summit code is:" - {code} not included
    validity: str
    ignore: str


_COPY: dict[str, _Copy] = {
    "en": _Copy(
        subject_activation="{code} is your AI Summit verification code",
        subject_login="{code} is your AI Summit sign-in code",
        subject_reset="{code} is your AI Summit password reset code",
        greeting_reset="Your AI Summit password reset code is:",
        ignore_reset="If you did not ask to reset your password, ignore this email: your password has not changed.",
        greeting="Your AI Summit code is:",
        validity="It expires in 10 minutes.",
        ignore="If you did not request it, you can safely ignore this email.",
    ),
    "az": _Copy(
        subject_activation="{code} — AI Summit təsdiq kodunuzdur",
        subject_login="{code} — AI Summit giriş kodunuzdur",
        subject_reset="{code} — AI Summit şifrə bərpası kodunuzdur",
        greeting_reset="AI Summit şifrə bərpası kodunuz:",
        ignore_reset="Şifrənin bərpasını siz istəməmisinizsə, bu məktuba məhəl qoymayın: şifrəniz dəyişməyib.",
        greeting="AI Summit kodunuz:",
        validity="Kodun müddəti 10 dəqiqədir.",
        ignore="Bu kodu siz tələb etməmisinizsə, bu məktuba məhəl qoymayın.",
    ),
    "ru": _Copy(
        subject_activation="{code} — ваш код подтверждения AI Summit",
        subject_login="{code} — ваш код для входа в AI Summit",
        subject_reset="{code} — ваш код для сброса пароля AI Summit",
        greeting_reset="Ваш код для сброса пароля AI Summit:",
        ignore_reset="Если вы не запрашивали сброс пароля, проигнорируйте это письмо: ваш пароль не изменился.",
        greeting="Ваш код AI Summit:",
        validity="Код действует 10 минут.",
        ignore="Если вы не запрашивали код, просто проигнорируйте это письмо.",
    ),
}


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


def render_otp_email(language: str, purpose: OtpPurpose, code: str) -> RenderedEmail:
    lang: Language = language if language in _COPY else DEFAULT_LANGUAGE  # type: ignore[assignment]
    copy = _COPY[lang]
    if purpose == OtpPurpose.STAFF_PASSWORD_RESET:
        subject_template, greeting, ignore = (
            copy.subject_reset,
            copy.greeting_reset,
            copy.ignore_reset,
        )
    elif purpose == OtpPurpose.ACTIVATION:
        subject_template, greeting, ignore = copy.subject_activation, copy.greeting, copy.ignore
    else:
        subject_template, greeting, ignore = copy.subject_login, copy.greeting, copy.ignore
    subject = subject_template.format(code=code)
    text = f"{greeting} {code}\n\n{copy.validity} {ignore}\n"
    safe_code = html_lib.escape(code)
    html = (
        f'<div lang="{lang}" style="font-family:Arial,Helvetica,sans-serif;'
        'font-size:16px;color:#111;">'
        f"<p>{html_lib.escape(greeting)}</p>"
        f'<p style="font-size:28px;font-weight:bold;letter-spacing:4px;">{safe_code}</p>'
        f"<p>{html_lib.escape(copy.validity)} {html_lib.escape(ignore)}</p></div>"
    )
    return RenderedEmail(subject=subject, text=text, html=html)
