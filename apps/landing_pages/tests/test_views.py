from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.users.factories import UserFactory
from apps.workspaces.factories import MembershipFactory, WorkspaceFactory
from apps.workspaces.roles import ROLE_MEMBER


class TestHomeView(TestCase):
    def test_unauthenticated_user_is_redirected_to_login(self):
        """This is a private instance: signup is disabled, so a stranger has
        nothing to act on in the marketing page. Sending them straight to the
        sign-in form removes a pointless click."""
        response = self.client.get(reverse("landing_pages:home"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    @override_settings(LANDING_PAGE_FOR_ANONYMOUS=True)
    def test_the_landing_page_can_be_restored_for_a_public_deployment(self):
        """A public instance wants the marketing page as its front door."""
        response = self.client.get(reverse("landing_pages:home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "landing_pages/landing_page.html")

    def test_authenticated_user_with_no_workspace_gets_landing_page(self):
        user = UserFactory()
        self.client.force_login(user)
        response = self.client.get(reverse("landing_pages:home"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "landing_pages/landing_page.html")

    def test_authenticated_user_with_workspace_redirects(self):
        user = UserFactory()
        workspace = WorkspaceFactory()
        MembershipFactory(workspace=workspace, user=user, role=ROLE_MEMBER)
        self.client.force_login(user)
        response = self.client.get(reverse("landing_pages:home"))
        self.assertRedirects(response, workspace.get_absolute_url(), fetch_redirect_response=False)


class TestFaviconView(TestCase):
    def test_get_returns_svg(self):
        response = self.client.get(reverse("landing_pages:favicon"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")

    def test_post_returns_405(self):
        response = self.client.post(reverse("landing_pages:favicon"))
        self.assertEqual(response.status_code, 405)

    def test_put_returns_405(self):
        response = self.client.put(reverse("landing_pages:favicon"))
        self.assertEqual(response.status_code, 405)

    def test_cache_control_header(self):
        response = self.client.get(reverse("landing_pages:favicon"))
        cache_control = response["Cache-Control"]
        self.assertIn("max-age=86400", cache_control)
        self.assertIn("immutable", cache_control)
        self.assertIn("public", cache_control)


class TestHealthCheckView(TestCase):
    @override_settings(HEALTH_CHECK_TOKENS=["secret-token"])
    def test_no_token_returns_404(self):
        response = self.client.get(reverse("landing_pages:health_check"))
        self.assertEqual(response.status_code, 404)

    @override_settings(HEALTH_CHECK_TOKENS=["secret-token"])
    def test_invalid_token_returns_404(self):
        response = self.client.get(reverse("landing_pages:health_check"), {"token": "wrong"})
        self.assertEqual(response.status_code, 404)

    @override_settings(HEALTH_CHECK_TOKENS=["secret-token"])
    @patch("health_check.contrib.redis.backends.RedisHealthCheck.check_status")
    @patch("health_check.contrib.celery.backends.CeleryHealthCheck.check_status")
    def test_valid_token_returns_200(self, _mock_celery_check, _mock_redis_check):
        """Both external backends are stubbed.

        MainView returns 500 if any registered backend reports a failure, so an
        unmocked backend would make this assert that the developer's machine
        happens to be running Redis and Celery - not that the view authorises the
        token and renders. The token check is what this test is about.
        """
        response = self.client.get(reverse("landing_pages:health_check"), {"token": "secret-token"})
        self.assertEqual(response.status_code, 200)


class TestStaticPages(TestCase):
    def test_terms_page(self):
        response = self.client.get(reverse("landing_pages:terms"))
        self.assertEqual(response.status_code, 200)

    def test_privacy_page(self):
        response = self.client.get(reverse("landing_pages:privacy"))
        self.assertEqual(response.status_code, 200)

    def test_400_page(self):
        response = self.client.get(reverse("landing_pages:400"))
        self.assertEqual(response.status_code, 200)

    def test_403_page(self):
        response = self.client.get(reverse("landing_pages:403"))
        self.assertEqual(response.status_code, 200)

    def test_404_page(self):
        response = self.client.get(reverse("landing_pages:404"))
        self.assertEqual(response.status_code, 200)

    def test_429_page(self):
        response = self.client.get(reverse("landing_pages:429"))
        self.assertEqual(response.status_code, 200)

    def test_500_page(self):
        response = self.client.get(reverse("landing_pages:500"))
        self.assertEqual(response.status_code, 200)
