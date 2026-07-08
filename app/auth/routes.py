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
from ..models import User, utcnow
from . import tokens
from .email_utils import send_email
from .forms import LoginForm, MagicLinkForm, RegisterForm

bp = Blueprint("auth", __name__, url_prefix="/auth")

# In-process per-IP rate limits on the public auth endpoints, to blunt bots
# fuzzing registration, password brute-forcing, and magic-link email
# bombing (repeatedly spamming a real user's inbox with sign-in links).
# Deliberately simple (in-memory dict, no external dependency) — sufficient
# for the single-worker deployment this app runs; a multi-worker deployment
# would need a shared store instead. Separate buckets per endpoint so
# hammering one doesn't lock a legitimate user out of another.
_RATE_LIMIT_WINDOW_SECONDS = 600
_rate_limit_buckets: dict[str, dict[str, list[float]]] = {}


def _rate_limited(bucket: str, key: str, max_attempts: int) -> bool:
    now = time.monotonic()
    attempts_by_key = _rate_limit_buckets.setdefault(bucket, {})
    attempts = [t for t in attempts_by_key.get(key, []) if now - t < _RATE_LIMIT_WINDOW_SECONDS]
    attempts.append(now)
    attempts_by_key[key] = attempts
    return len(attempts) > max_attempts


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))
    form = RegisterForm()
    if form.is_submitted() and _rate_limited("register", request.remote_addr or "unknown", 5):
        flash("Too many registration attempts — please try again later.", "danger")
    elif form.validate_on_submit():
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

            from ..models import EditionRecipient

            db.session.add(EditionRecipient(user_id=user.id, email=email, confirmed_at=utcnow()))
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
    if form.submit.data and _rate_limited("login", request.remote_addr or "unknown", 10):
        flash("Too many sign-in attempts — please try again later.", "danger")
    elif form.submit.data and form.validate_on_submit():
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
    if _rate_limited("magic-link", request.remote_addr or "unknown", 5):
        flash("Too many sign-in link requests — please try again later.", "danger")
        return redirect(url_for("auth.login"))
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
