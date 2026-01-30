from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from app.core.config import settings
from app.core.db import engine, init_db
from app.main import app
from app.models import (
    Adjustment,
    Item,
    Remittance,
    RemittanceLine,
    Settlement,
    TimeSegment,
    User,
    WorkLog,
)
from tests.utils.user import authentication_token_from_email
from tests.utils.utils import get_superuser_token_headers


@pytest.fixture(scope="session", autouse=True)
def db() -> Generator[Session, None, None]:
    with Session(engine) as session:
        init_db(session)
        yield session
        # Delete in order to respect foreign key constraints
        # Use explicit deletes to ensure they execute
        session.execute(delete(RemittanceLine))
        session.execute(delete(Remittance))
        session.execute(delete(Settlement))
        session.execute(delete(Adjustment))
        session.execute(delete(TimeSegment))
        session.execute(delete(WorkLog))
        session.execute(delete(Item))
        # Only delete non-superuser users to avoid deleting the init_db superuser
        # But first ensure all their worklogs are deleted (done above)
        session.execute(delete(User).where(User.is_superuser == False))  # noqa: E712
        session.commit()


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def superuser_token_headers(client: TestClient) -> dict[str, str]:
    return get_superuser_token_headers(client)


@pytest.fixture(scope="module")
def normal_user_token_headers(client: TestClient, db: Session) -> dict[str, str]:
    return authentication_token_from_email(
        client=client, email=settings.EMAIL_TEST_USER, db=db
    )
