from __future__ import absolute_import, unicode_literals

from django_downloadview.test import assert_download_response

from mayan.apps.common.tests import GenericViewTestCase

from ..models import Key
from ..permissions import permission_key_download, permission_key_upload

from .literals import TEST_KEY_DATA, TEST_KEY_FINGERPRINT
from .mixins import KeyTestMixin


class KeyViewTestMixin(object):
    def _request_test_key_download_view(self):
        return self.get(
            viewname='django_gpg:key_download', kwargs={'pk': self.test_key.pk}
        )

    def _request_test_key_upload_view(self):
        return self.post(
            viewname='django_gpg:key_upload', data={'key_data': TEST_KEY_DATA}
        )


class KeyViewTestCase(KeyTestMixin, KeyViewTestMixin, GenericViewTestCase):
    def test_key_download_view_no_permission(self):
        self._create_test_key()

        response = self._request_test_key_download_view()
        self.assertEqual(response.status_code, 403)

    def test_key_download_view_with_permission(self):
        self.expected_content_types = ('application/octet-stream; charset=utf-8',)

        self._create_test_key()

        self.grant_access(
            obj=self.test_key, permission=permission_key_download
        )

        response = self._request_test_key_download_view()
        assert_download_response(
            self, response=response, content=self.test_key.key_data,
            basename=self.test_key.key_id,
        )

    def test_key_upload_view_no_permission(self):
        response = self._request_test_key_upload_view()
        self.assertEqual(response.status_code, 403)

        self.assertEqual(Key.objects.count(), 0)

    def test_key_upload_view_with_permission(self):
        self.grant_permission(permission=permission_key_upload)

        response = self._request_test_key_upload_view()
        self.assertEqual(response.status_code, 302)

        self.assertEqual(Key.objects.count(), 1)
        self.assertEqual(
            Key.objects.first().fingerprint, TEST_KEY_FINGERPRINT
        )
