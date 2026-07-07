import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db as _db
from app.models import ApiKey, NewsItem, Tag, User


def give_edition_key(db, user, secret: str = "sk-or-test", model: str | None = None) -> ApiKey:
    """Create a personal ApiKey for ``user`` and select it for editions —
    the test equivalent of adding a key on /keys and clicking "Use for
    editions"."""
    key = ApiKey(owner_user_id=user.id, label="Test edition key", model=model)
    key.set_key(secret)
    db.session.add(key)
    db.session.commit()
    user.edition_api_key_id = key.id
    db.session.commit()
    return key


@pytest.fixture
def app():
    from app.auth.routes import _register_attempts
    _register_attempts.clear()
    app = create_app(TestConfig)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user(db):
    u = User(username="alice", email="alice@example.com", email_verified=True)
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def admin(db):
    u = User(username="boss", email="admin@example.com", email_verified=True)
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def auth_client(client, user):
    client.post(
        "/auth/login",
        data={"email": user.email, "password": "password123", "submit": "Sign in"},
        follow_redirects=True,
    )
    return client


@pytest.fixture
def admin_client(client, admin):
    client.post(
        "/auth/login",
        data={"email": admin.email, "password": "password123", "submit": "Sign in"},
        follow_redirects=True,
    )
    return client


@pytest.fixture
def sample_tags(db):
    tags = [
        Tag(name="LLMs", keywords=["language model", "gpt", "llm", "chatbot"],
            explanation="Large language models.", scope="global"),
        Tag(name="Robotics", keywords=["robot", "humanoid", "actuator"],
            explanation="Physical robots.", scope="global"),
    ]
    db.session.add_all(tags)
    db.session.commit()
    return tags


@pytest.fixture
def sample_items(db):
    items = [
        NewsItem(
            dedup_hash=NewsItem.make_hash("OpenAI releases new GPT model", "http://x/1"),
            title="OpenAI releases new GPT model",
            url="http://x/1",
            summary_text="A new large language model chatbot from OpenAI sets benchmarks.",
        ),
        NewsItem(
            dedup_hash=NewsItem.make_hash("Boston Dynamics humanoid robot", "http://x/2"),
            title="Boston Dynamics humanoid robot",
            url="http://x/2",
            summary_text="A new humanoid robot with advanced actuators was demonstrated.",
        ),
    ]
    db.session.add_all(items)
    db.session.commit()
    return items
