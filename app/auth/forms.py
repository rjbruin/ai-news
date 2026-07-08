"""WTForms for authentication."""
from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import BooleanField, HiddenField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional, ValidationError

# RFC 2606 reserved domains — never real, deliverable mailboxes. Public
# scanners/bots routinely replay this repo's own test fixtures (which use
# newbie@example.com) against any live registration endpoint they find, so
# registering with one just creates noise: an account nobody can ever
# verify, and a verification email that clutters an inbox or bounces.
RESERVED_EMAIL_DOMAINS = {"example.com", "example.net", "example.org", "example.edu"}


def _reject_reserved_domain(form, field) -> None:
    domain = field.data.rsplit("@", 1)[-1].strip().lower()
    if domain in RESERVED_EMAIL_DOMAINS:
        raise ValidationError("That email domain can't receive mail — please use a real address.")


class RegisterForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(3, 80)])
    email = StringField(
        "Email",
        validators=[DataRequired(), Email(), Length(max=255), _reject_reserved_domain],
    )
    password = PasswordField(
        "Password (optional — you can use magic-link login)",
        validators=[Optional(), Length(min=8)],
    )
    confirm = PasswordField(
        "Confirm password", validators=[Optional(), EqualTo("password")]
    )
    invite_code = HiddenField()
    submit = SubmitField("Create account")


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign in")


class MagicLinkForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Email me a sign-in link")
