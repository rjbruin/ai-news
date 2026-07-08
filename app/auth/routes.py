"""Authentication routes: register, password login, magic-link login, verify."""
from __future__ import annotations

import time

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_user, logout_user

from ..extensions import db
from ..models import AdminSettings, EditionRecipient, Invite, User, utcnow
from . import tokens
from .email_utils import send_email
from .forms import LoginForm, MagicLinkForm, RegisterForm

bp = Blueprint("auth", __name__, url_prefix="/auth")

# In-process per-IP rate limit for registration attempts, to blunt bots
# fuzzing the public /auth/register form. Deliberately simple (in-memory
# dict, no external dependency) — sufficient for the single-worker
# deployment this app runs; a multi-worker deployment would need a shared
# store instead.
_REGISTER_WINDOW_SECONDS = 600
_REGISTER_MAX_ATTEMPTS = 5
_register_attempts: dict[str, list[float]] = {}


def _register_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    attempts = [t for t in _register_attempts.get(ip, []) if now - t < _REGISTER_WINDOW_SECONDS]
    attempts.append(now)
    _register_attempts[ip] = attempts
    return len(attempts) > _REGISTER_MAX_ATTEMPTS


def _find_usable_invite(code: str | None) -> Invite | None:
    if not code:
        return None
    invite = Invite.query.filter_by(code=code).first()
    return invite if invite and invite.is_usable else None


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))

    registration_open = AdminSettings.get().registration_open
    invite_code = request.args.get("invite") if request.method == "GET" else request.form.get("invite_code")
    invite = None if registration_open else _find_usable_invite(invite_code)

    if request.method == "GET" and not registration_open and invite is None:
        # No form to show at all — an invite (or open registration) is required
        # before anyone can even attempt to register. POST falls through to the
        # form-validation branch below instead, so a submitted-but-invalid/
        # exhausted invite gets a specific error rather than this generic page.
        return render_template("auth/register_closed.html")

    form = RegisterForm(invite_code=invite_code)
    if form.is_submitted() and _register_rate_limited(request.remote_addr or "unknown"):
        flash("Too many registration attempts — please try again later.", "danger")
    elif form.validate_on_submit():
        # Re-resolve the invite from the submitted hidden field — request.args
        # (used above for the GET-time check) isn't present on POST.
        invite = None if registration_open else _find_usable_invite(form.invite_code.data)
        if not registration_open and invite is None:
            flash("That invite link is invalid or has already been used up.", "danger")
            return render_template("auth/register.html", form=form)

        email = form.email.data.strip().lower()
        if User.query.filter_by(email=email).first():
            flash("That email is already registered.", "danger")
        elif User.query.filter_by(username=form.username.data.strip()).first():
            flash("That username is taken.", "danger")
        else:
            user = User(username=form.username.data.strip(), email=email)
            if form.password.data:
                user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()

            db.session.add(EditionRecipient(user_id=user.id, email=email, confirmed_at=utcnow()))
            if invite is not None:
                invite.uses_count += 1
            db.session.commit()

            _send_verification(user)
            flash(
                "Account created. Check your email to verify your address.",
                "success",
            )
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))
    form = LoginForm()
    magic_form = MagicLinkForm()
    if form.submit.data and form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(form.password.data):
            _do_login(user, remember=form.remember.data)
            return redirect(_safe_next() or url_for("web.dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("auth/login.html", form=form, magic_form=magic_form)


@bp.route("/magic-link", methods=["POST"])
def magic_link():
    form = MagicLinkForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()
        # Always show the same message to avoid leaking which emails exist.
        if user:
            token = tokens.generate(user, purpose="login")
            link = url_for("auth.magic_login", token=token, _external=True)
            send_email(
                user.email,
                "Your Dispatch sign-in link",
                f"Click to sign in (valid 30 minutes):\n\n{link}\n",
            )
    flash("If that email exists, a sign-in link has been sent.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/magic/<token>")
def magic_login(token: str):
    user = tokens.verify(token, purpose="login")
    if user is None:
        flash("That sign-in link is invalid or has expired.", "danger")
        return redirect(url_for("auth.login"))
    user.email_verified = True  # using the link proves email ownership
    _do_login(user)
    return redirect(url_for("web.dashboard"))


@bp.route("/verify/<token>")
def verify_email(token: str):
    user = tokens.verify(token, purpose="verify")
    if user is None:
        flash("That verification link is invalid or has expired.", "danger")
        return redirect(url_for("auth.login"))
    user.email_verified = True
    db.session.commit()
    flash("Email verified — you can now sign in.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/logout")
def logout():
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


# ───────────────────────── helpers ─────────────────────────
def _do_login(user: User, remember: bool = True) -> None:
    user.last_login = utcnow()
    db.session.commit()
    login_user(user, remember=remember)


def _send_verification(user: User) -> None:
    token = tokens.generate(user, purpose="verify")
    link = url_for("auth.verify_email", token=token, _external=True)
    send_email(
        user.email,
        "Verify your Dispatch account",
        f"Welcome to Dispatch! Verify your email:\n\n{link}\n",
    )


def _safe_next() -> str | None:
    nxt = request.args.get("next")
    # Only allow relative redirects.
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return None
