import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")

_django_app = get_asgi_application()


class LogAuthMiddleware:
	def __init__(self, app):
		self.app = app

	async def __call__(self, scope, receive, send):
		if scope.get("type") == "http":
			try:
				headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
				auth = headers.get("authorization")
			except Exception:
				auth = None
			print(f"Core Incoming Authorization: {auth}")
		await self.app(scope, receive, send)


application = LogAuthMiddleware(_django_app)