import logging
import sys

from django.conf import settings
from django.core.management import execute_from_command_line
from django.http import JsonResponse
from django.urls import path

settings.configure(
    DEBUG=True,
    ALLOWED_HOSTS=["*"],
    ROOT_URLCONF=__name__,
    SECRET_KEY="dev-only-not-a-secret",
    MIDDLEWARE=[],
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "plain": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
        },
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": "/logs/django.log",
                "formatter": "plain",
            },
        },
        "root": {
            "handlers": ["file"],
            "level": "INFO",
        },
    },
)

logger = logging.getLogger("app")


def index(request):
    logger.info("handled request to /")
    return JsonResponse({"message": "hello from django"})


urlpatterns = [path("", index)]

if __name__ == "__main__":
    execute_from_command_line(sys.argv)