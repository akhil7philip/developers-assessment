from fastapi import APIRouter, Depends, Query
from pydantic.networks import EmailStr

from app.api.deps import get_current_active_superuser
from app.api.routes.utils.service import UtilsService
from app.models import Message

router = APIRouter(prefix="/utils", tags=["utils"])


@router.post(
    "/test-email/",
    dependencies=[Depends(get_current_active_superuser)],
    status_code=201,
)
def test_email(email_to: EmailStr = Query(...)) -> Message:
    """
    Test emails.
    """
    return UtilsService.test_email(email_to)


@router.get("/health-check/")
def health_check() -> bool:
    return UtilsService.health_check()
