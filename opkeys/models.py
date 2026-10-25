import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.db import models as django_models


class OpSession(models.Model):
    """Represents a short-lived operations key session.

    The raw key is shown only once at creation; the DB stores a keyed HMAC hash.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE
    )
    key_hash = models.CharField(max_length=128, db_index=True)
    ops = django_models.JSONField(default=list)  # list of allowed operation strings, e.g. ["POST:/api/admin/products"]
    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used = models.DateTimeField(null=True, blank=True)
    revoked = models.BooleanField(default=False)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "opkeys_opsession"

    def is_expired(self):
        if self.expires_at is None:
            return False
        return timezone.now() >= self.expires_at

    def is_valid(self):
        return (not self.revoked) and (not self.is_expired())

    def touch(self):
        self.last_used = timezone.now()
        self.save(update_fields=["last_used"]) 

    def __str__(self):
        return f"OpSession({self.id}, user={self.user_id}, revoked={self.revoked})"
