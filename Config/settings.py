"""
Django settings for the Traceability project.

This file contains configuration for installed apps, middleware, templates, static files,
database, authentication, internationalization, and security settings.
"""

from pathlib import Path
import sys
from cryptography.fernet import Fernet
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY")

DEBUG = False
if sys.argv[1] == "runserver":
    DEBUG = True

ALLOWED_HOSTS = ["*"]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "Core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "Config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "Core.views.context_processors.employee_name",
                "Core.views.context_processors.user_role",
            ],
        },
    },
]

WSGI_APPLICATION = "Config.wsgi.application"
SESSION_ENGINE = "django.contrib.sessions.backends.file"

# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

USERNAME = config("USER_NAME")
PASSWORD_ENCRYPTED = config("PASSWORD")
AUTH_DB = config("AUTH_DB")
HOST = config("HOST")
PRINTER_IP = config("PRINTER_IP")

AI_API_URL = config("AI_API_URL")
AI_MODEL = config("AI_MODEL")
AI_API_KEY = config("AI_API_KEY")

COE_DB = config("COE_DB")

PGR_DB = config("PGR_DB")

VVP_DB = config("VVP_DB")

PPM_DB = config("PPM_DB")

KPM_DB = config("KPM_DB")

# Decrypt
fernet = Fernet(SECRET_KEY.encode())
PASSWORD = fernet.decrypt(PASSWORD_ENCRYPTED.encode()).decode()

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Asia/Kolkata"

USE_I18N = True

USE_TZ = True


STATIC_URL = "/static_files/"
STATIC_ROOT = f"{BASE_DIR}/static_files/"
MEDIA_URL = "/media/"
MEDIA_ROOT = f"{BASE_DIR}/media/"

APILOGIN = config("APILOGIN")
BAPI_API = config("BAPI_API")

LOGOUT_REDIRECT_URL = "/"

X_FRAME_OPTIONS = "SAMEORIGIN"


if not DEBUG:
    SECURE_HSTS_MAX_AGE = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Strict"
    CSRF_COOKIE_SECURE = True
    CSRF_COOKIE_HTTPONLY = True
    CSRF_COOKIE_SAMESITE = "Strict"
    SECURE_SSL_REDIRECT = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "strict-origin"
    SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
    RL_KEY = "header:x-real-ip"
    X_XSS_PROTECTION = "1; mode=block"

    SECURE_CROSS_ORIGIN_RESOURCE_POLICY = "same-origin"
    SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_NAME = "sessionid_app2"
