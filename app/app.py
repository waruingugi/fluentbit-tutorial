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
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                "rename_fields": {"asctime": "time", "levelname": "level", "name": "logger"},
            },
            "plain": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
        },
        "handlers": {
            "app_file": {
                "class": "logging.FileHandler",
                "filename": "/logs/app.log",
                "formatter": "json",
            },
            "access_file": {
                "class": "logging.FileHandler",
                "filename": "/logs/access.log",
                "formatter": "plain",
            },
            "error_file": {
                "class": "logging.FileHandler",
                "filename": "/logs/error.log",
                "formatter": "plain",
            },
        },
        "loggers": {
            "app": {"handlers": ["app_file"], "level": "INFO", "propagate": False},
            "django.server": {"handlers": ["access_file"], "level": "INFO", "propagate": False},
            "errors": {"handlers": ["error_file"], "level": "ERROR", "propagate": False},
        },
    },
)

logger = logging.getLogger("app")
err_logger = logging.getLogger("errors")


def index(request):
    logger.info("handled request to /")
    return JsonResponse({"message": "hello from django"})


def boom(request):
    try:
        result = 1 / 0
    except Exception:
        err_logger.exception("failed to handle /boom")
    return JsonResponse({"detail": "an error was logged"}, status=500)


urlpatterns = [
    path("", index),
    path("boom", boom),
]

if __name__ == "__main__":
    execute_from_command_line(sys.argv)