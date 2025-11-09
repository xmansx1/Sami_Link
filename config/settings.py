# config/settings.py
# إعدادات مشروع SamiLink — وضع التطوير افتراضيًا على SQLite، ويمكن التبديل إلى Postgres عبر .env

from pathlib import Path
import os
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

# تحميل .env إن توفر (اختياري وآمن)
if ENV_FILE.exists():
    try:
        from dotenv import load_dotenv  # python-dotenv
        load_dotenv(ENV_FILE)
    except Exception as e:
        if os.getenv("DEBUG", "True") == "True":
            print(f"[settings] .env not fully loaded: {e}")

def env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, str(default))
    return val.lower() in ("1", "true", "yes", "on")

def env_list(key: str, default: str = "") -> list[str]:
    raw = os.getenv(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]

# --- القيم الأساسية ---
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")  # غيّرها في الإنتاج
DEBUG = env_bool("DEBUG", True)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "")

# --- التطبيقات ---
INSTALLED_APPS = [
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # طرف ثالث
    "rest_framework",
    "django_filters",
    "channels",              # WebSockets / Django Channels
    # ملاحظة: أضِف 'corsheaders' إذا أردت CORS واجهة/واجهة
    # "corsheaders",
    # "rest_framework.authtoken",

    # تطبيقات المشروع
    "accounts",
    "profiles",
    "marketplace",
    "finance",
    "disputes",
    "uploads",
    "notifications",
    "core",
    "website",
    "agreements.apps.AgreementsConfig",
]
AUTH_USER_MODEL = "accounts.User"
PHONE_DEFAULT_COUNTRY_CODE = os.getenv("PHONE_DEFAULT_COUNTRY_CODE", "966")

# --- الوسائط / Middleware ---
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # لخدمة static في الإنتاج
    # "corsheaders.middleware.CorsMiddleware",     # فعّل إذا استخدمت CORS
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"  # مطلوب لـ Channels

# --- القوالب ---
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # يمكن إضافة معالجات سياق لاحقًا (مثلاً سياسة السوق أو إعدادات الهوية)
            ],
        },
    },
]

# --- قاعدة البيانات ---
# افتراضيًا: SQLite للتطوير. للتبديل إلى Postgres ضع DB_ENGINE=postgres في .env مع بقية القيم.
DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").lower()

if DB_ENGINE == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "samilink"),
            "USER": os.getenv("DB_USER", "postgres"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
            "OPTIONS": {
                # "sslmode": "require",  # فعّلها عند مزوّد يستلزم SSL
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# --- الكاش (تحسين الأداء) ---
CACHES = {
    "default": {
        "BACKEND": os.getenv(
            "CACHE_BACKEND",
            "django.core.cache.backends.locmem.LocMemCache"
        ),
        "LOCATION": "samilink-local",
        "TIMEOUT": int(os.getenv("CACHE_TIMEOUT", "300")),
    }
}

# --- قنوات الويب (Channels) ---
# استخدام Redis في الإنتاج و InMemory في التطوير تلقائيًا
REDIS_URL = os.getenv("REDIS_URL", "")
if REDIS_URL:
    parsed = urlparse(REDIS_URL)
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [(parsed.hostname, parsed.port or 6379)]},
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }

# --- التدويل والوقت ---
LANGUAGE_CODE = "ar"
TIME_ZONE = "Asia/Riyadh"
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]

# --- الملفات الساكنة والإعلام ---
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# WhiteNoise
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
WHITENOISE_KEEP_ONLY_HASHED_FILES = True

# --- الأمان (فعّل القيم الصارمة بالإنتاج) ---
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_HTTPONLY = env_bool("SESSION_COOKIE_HTTPONLY", True)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_HTTPONLY = env_bool("CSRF_COOKIE_HTTPONLY", True)
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = os.getenv("CSRF_COOKIE_SAMESITE", "Lax")
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", not DEBUG)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
REFERRER_POLICY = os.getenv("REFERRER_POLICY", "strict-origin-when-cross-origin")
USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", True)

# --- البريد (SMTP) — يُستخدم لاحقًا في الإشعارات ---
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.example.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "30"))
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "SamiLink <no-reply@samilink.sa>")
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
ADMINS = [("GM", os.getenv("ADMIN_EMAIL", "admin@samilink.sa"))]

# --- تسجيل الأخطاء ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{levelname}] {asctime} {name} :: {message}", "style": "{"},
        "simple": {"format": "[{levelname}] {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# --- مدققات كلمات المرور ---
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- الجلسات ---
SESSION_COOKIE_AGE = int(os.getenv("SESSION_COOKIE_AGE", str(60 * 60 * 24 * 7)))  # أسبوع
SESSION_SAVE_EVERY_REQUEST = env_bool("SESSION_SAVE_EVERY_REQUEST", False)

# --- مصادقة وتوجيه ---
LOGIN_URL = os.getenv("LOGIN_URL", "accounts:login")
LOGIN_REDIRECT_URL = os.getenv("LOGIN_REDIRECT_URL", "website:home")
LOGOUT_REDIRECT_URL = os.getenv("LOGOUT_REDIRECT_URL", "website:home")

# --- DRF ---
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        # "rest_framework.authentication.TokenAuthentication",  # فعّلها لو استخدمت authtoken
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": int(os.getenv("DRF_PAGE_SIZE", "25")),
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer" if DEBUG else "rest_framework.renderers.JSONRenderer",
    ],
}

APPEND_SLASH = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# =====================================================================
# إعدادات سياسة السوق/المنصة (للتحكم في السلوك بدون تعديل كود الفيوز)
# =====================================================================

# الفجوة 2: إخفاء بيانات العميل أثناء العروض
# عند تفعيلها، ستُخفي المنصة بيانات الاتصال للعميل وتقُم بتعمية النصوص/الروابط
HIDE_CLIENT_CONTACT_DURING_OFFERS = env_bool("HIDE_CLIENT_CONTACT_DURING_OFFERS", True)
# نافذة استقبال العروض (أيام)
OFFER_WINDOW_DAYS = int(os.getenv("OFFER_WINDOW_DAYS", "5"))
# كل تقني مسموح له بعرض واحد فقط على الطلب
ONE_OFFER_PER_TECH = env_bool("ONE_OFFER_PER_TECH", True)

# أنماط التعمية لاكتشاف/إخفاء بيانات الاتصال في نصوص الطلب خلال نافذة العروض
# يمكن للفيوز/الخدمات استخدام هذه الأنماط لتجريد/إخفاء (Emails/Phones/Links/Usernames)
CONTACT_SANITIZATION_PATTERNS = [
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",   # Emails
    r"(?<!\d)(?:\+?\d[\d\s\-]{7,}\d)",                   # أرقام هواتف عامة
    r"(?:https?://|www\.)\S+",                           # روابط
    r"(?:@|at\s+)?[A-Za-z0-9_]{3,}",                     # معرفات بسيطة
]

# رسوم/ضرائب المنصة (للعرض والحساب)
PLATFORM_FEE_DEFAULT = float(os.getenv("PLATFORM_FEE_DEFAULT", "0.10"))  # 10% افتراضيًا
VAT_RATE = float(os.getenv("VAT_RATE", "0.15"))                           # 15%

# تعريفات رسمية للاستخدام داخل الاتفاقية والفواتير
PLATFORM_OFFICIAL_NAME = os.getenv("PLATFORM_OFFICIAL_NAME", "منصة سامي لينك")
PLATFORM_CR_NUMBER = os.getenv("PLATFORM_CR_NUMBER", "7050062491")

# سياسة النزاعات: عند فتح نزاع يتم تجميد الصرف تلقائيًا
FREEZE_PAYOUT_ON_DISPUTE = env_bool("FREEZE_PAYOUT_ON_DISPUTE", True)

# خيارات CORS (اختياري — فعّل corsheaders أعلاه إن أردت)
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "")
CORS_ALLOW_CREDENTIALS = env_bool("CORS_ALLOW_CREDENTIALS", True)
