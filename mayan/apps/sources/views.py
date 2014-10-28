from __future__ import absolute_import

import tempfile

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _

from acls.models import AccessEntry
from common.utils import encapsulate
from common.views import MultiFormView
from documents.models import DocumentType, Document
from documents.permissions import (PERMISSION_DOCUMENT_CREATE,
                                   PERMISSION_DOCUMENT_NEW_VERSION)
from metadata.api import decode_metadata_from_url, metadata_repr_as_list
from permissions.models import Permission

from .forms import (NewDocumentForm, NewVersionForm, SourceTransformationForm,
                    SourceTransformationForm_create)
from .literals import (SOURCE_CHOICE_STAGING, SOURCE_CHOICE_WEB_FORM,
                       SOURCE_UNCOMPRESS_CHOICE_ASK, SOURCE_UNCOMPRESS_CHOICE_Y)
from .models import (InteractiveSource, Source, StagingFolderSource,
                     SourceTransformation, WebFormSource)
from .permissions import (PERMISSION_SOURCES_SETUP_CREATE,
                          PERMISSION_SOURCES_SETUP_DELETE,
                          PERMISSION_SOURCES_SETUP_EDIT,
                          PERMISSION_SOURCES_SETUP_VIEW)
from .tasks import task_upload_document, task_upload_new_version
from .utils import get_class, get_form_class, get_upload_form_class


def document_create_siblings(request, document_id):
    Permission.objects.check_permissions(request.user, [PERMISSION_DOCUMENT_CREATE])

    document = get_object_or_404(Document, pk=document_id)
    query_dict = {}
    for pk, metadata in enumerate(document.metadata.all()):
        query_dict['metadata%s_id' % pk] = metadata.metadata_type_id
        query_dict['metadata%s_value' % pk] = metadata.value

    query_dict['document_type_id'] = document.document_type_id

    url = reverse('sources:upload_interactive')
    return HttpResponseRedirect('%s?%s' % (url, urlencode(query_dict)))


def conditional_highlight_factory(obj):
    return lambda context: context['source'].source_type == obj.source_type and context['source'].pk == obj.pk


def get_tab_link_for_source(source, document=None):
    if document:
        view = u'sources:upload_version'
        args = [document.pk, source.pk]
    else:
        view = u'sources:upload_interactive'
        args = [source.pk]

    return {
        'text': source.title,
        'view': view,
        'args': args,
        'keep_query': True,
        'conditional_highlight': conditional_highlight_factory(source),
    }


def get_active_tab_links(document=None):
    tab_links = []

    web_forms = WebFormSource.objects.filter(enabled=True)
    for web_form in web_forms:
        tab_links.append(get_tab_link_for_source(web_form, document))

    staging_folders = StagingFolderSource.objects.filter(enabled=True)
    for staging_folder in staging_folders:
        tab_links.append(get_tab_link_for_source(staging_folder, document))

    return {
        'tab_links': tab_links,
        SOURCE_CHOICE_WEB_FORM: web_forms,
        SOURCE_CHOICE_STAGING: staging_folders,
    }


class UploadBaseView(MultiFormView):
    template_name = 'main/generic_form.html'
    prefixes = {'source_form': 'source', 'document_form': 'document'}

    def dispatch(self, request, *args, **kwargs):
        if 'source_id' in kwargs:
            self.source = get_object_or_404(Source.objects.filter(enabled=True).select_subclasses(), pk=kwargs['source_id'])
        else:
            self.source = InteractiveSource.objects.filter(enabled=True).select_subclasses().first()

        return super(UploadBaseView, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(UploadBaseView, self).get_context_data(**kwargs)
        subtemplates_list = []

        context['source'] = self.source

        if InteractiveSource.objects.filter(enabled=True).count() == 0:
            subtemplates_list.append(
                {
                    'name': 'main/generic_subtemplate.html',
                    'context': {
                        'title': _(u'Upload sources'),
                        'paragraphs': [
                            _(u'No interactive document sources have been defined or none have been enabled.'),
                        ],
                    }
                })

        if isinstance(self.source, StagingFolderSource):
            try:
                staging_filelist = list(self.source.get_files())
            except Exception as exception:
                messages.error(self.request, exception)
                staging_filelist = []
            finally:
                subtemplates_list = [
                    {
                        'name': 'main/generic_multiform_subtemplate.html',
                        'context': {
                            'forms': context['forms'],
                        }
                    },
                    {
                        'name': 'main/generic_list_subtemplate.html',
                        'context': {
                            'title': _(u'Files in staging path'),
                            'object_list': staging_filelist,
                            'hide_link': True,
                        }
                    },
                ]
        else:
            subtemplates_list.append({
                'name': 'main/generic_multiform_subtemplate.html',
                'context': {
                    'forms': context['forms'],
                    'is_multipart': True
                },
            })

        context.update({
            'subtemplates_list': subtemplates_list,
            'temporary_navigation_links': {
                'form_header': {
                    'sources:upload_version': {
                        'links': self.tab_links['tab_links']
                    },
                    'sources:upload_interactive': {
                        'links': self.tab_links['tab_links']
                    }
                }
            },
        })

        return context


class UploadInteractiveView(UploadBaseView):

    def dispatch(self, request, *args, **kwargs):
        self.subtemplates_list = []

        Permission.objects.check_permissions(request.user, [PERMISSION_DOCUMENT_CREATE])

        self.document_type = get_object_or_404(DocumentType, pk=self.request.GET['document_type_id'])

        self.tab_links = get_active_tab_links()

        return super(UploadInteractiveView, self).dispatch(request, *args, **kwargs)

    def forms_valid(self, forms):
        if self.source.uncompress == SOURCE_UNCOMPRESS_CHOICE_ASK:
            expand = forms['source_form'].cleaned_data.get('expand')
        else:
            if self.source.uncompress == SOURCE_UNCOMPRESS_CHOICE_Y:
                expand = True
            else:
                expand = False

        uploaded_file = self.source.get_upload_file_object(self.request, forms['source_form'])

        file_object = uploaded_file.file
        temporary_file = tempfile.NamedTemporaryFile(delete=False)
        for chunk in file_object.chunks():
            temporary_file.write(chunk)

        temporary_file.close()
        file_object.close()

        try:
            self.source.clean_up_upload_file(uploaded_file)
        except Exception as exception:
            messages.error(self.request, exception)

        if not self.request.user.is_anonymous():
            user_id = self.request.user.pk
        else:
            user_id = None

        label = file_object.name
        if 'document_type_available_filenames' in forms['document_form'].cleaned_data:
            if forms['document_form'].cleaned_data['document_type_available_filenames']:
                label = forms['document_form'].cleaned_data['document_type_available_filenames'].filename

        task_upload_document.apply_async(kwargs=dict(
            source_id=self.source.pk,
            file_path=temporary_file.name,
            label=label,
            document_type_id=self.document_type.pk,
            expand=expand,
            metadata_dict_list=decode_metadata_from_url(self.request.GET),
            user_id=user_id,
            description=forms['document_form'].cleaned_data.get('description'),
            language=forms['document_form'].cleaned_data.get('language')
        ), queue='uploads')

        messages.success(self.request, _(u'New document queued for uploaded and will be available shortly.'))
        return HttpResponseRedirect(self.request.get_full_path())

    def create_source_form_form(self, **kwargs):
        return self.get_form_classes()['source_form'](
            prefix=kwargs['prefix'],
            source=self.source,
            show_expand=(self.source.uncompress == SOURCE_UNCOMPRESS_CHOICE_ASK),
            data=kwargs.get('data', None),
            files=kwargs.get('files', None),
        )

    def create_document_form_form(self, **kwargs):
        return self.get_form_classes()['document_form'](
            prefix=kwargs['prefix'],
            document_type=self.document_type,
            data=kwargs.get('data', None),
            files=kwargs.get('files', None),
        )

    def get_form_classes(self):
        return {'document_form': NewDocumentForm, 'source_form': get_upload_form_class(self.source.source_type)}

    def get_context_data(self, **kwargs):
        context = super(UploadInteractiveView, self).get_context_data(**kwargs)
        context['title'] = _(u'Upload a local document from source: %s') % self.source.title

        context.update(
            {
                'sidebar_subtemplates_list': [
                    {
                        'name': 'main/generic_subtemplate.html',
                        'context': {
                            'title': _(u'Current document type'),
                            'paragraphs': [self.document_type if self.document_type else _(u'None')],
                            'side_bar': True,
                        }
                    },
                    {
                        'name': 'main/generic_subtemplate.html',
                        'context': {
                            'title': _(u'Current metadata'),
                            'paragraphs': metadata_repr_as_list(decode_metadata_from_url(self.request.GET)),
                            'side_bar': True,
                        }
                    }
                ],
            }
        )
        return context


class UploadInteractiveVersionView(UploadBaseView):
    def dispatch(self, request, *args, **kwargs):

        self.subtemplates_list = []

        self.document = get_object_or_404(Document, pk=kwargs['document_pk'])
        try:
            Permission.objects.check_permissions(self.request.user, [PERMISSION_DOCUMENT_NEW_VERSION])
        except PermissionDenied:
            AccessEntry.objects.check_access(PERMISSION_DOCUMENT_NEW_VERSION, self.request.user, self.document)

        self.tab_links = get_active_tab_links(self.document)

        return super(UploadInteractiveVersionView, self).dispatch(request, *args, **kwargs)

    def forms_valid(self, forms):
        uploaded_file = self.source.get_upload_file_object(self.request, forms['source_form'])

        file_object = uploaded_file.file
        temporary_file = tempfile.NamedTemporaryFile(delete=False)
        for chunk in file_object.chunks():
            temporary_file.write(chunk)

        temporary_file.close()
        file_object.close()

        try:
            self.source.clean_up_upload_file(uploaded_file)
        except Exception as exception:
            messages.error(self.request, exception)

        if not self.request.user.is_anonymous():
            user_id = self.request.user.pk
        else:
            user_id = None

        task_upload_new_version.apply_async(kwargs=dict(
            source_id=self.source.pk,
            file_path=temporary_file.name,
            document_id=self.document.pk,
            user_id=user_id,
            version_update=forms['document_form'].cleaned_data.get('version_update'),
            comment=forms['document_form'].cleaned_data.get('comment')
        ), queue='uploads')

        messages.success(self.request, _(u'New document version queued for uploaded and will be available shortly.'))
        return HttpResponseRedirect(reverse('documents:document_version_list', args=[self.document.pk]))

    def create_source_form_form(self, **kwargs):
        return self.get_form_classes()['source_form'](
            prefix=kwargs['prefix'],
            source=self.source,
            show_expand=False,
            data=kwargs.get('data', None),
            files=kwargs.get('files', None),
        )

    def create_document_form_form(self, **kwargs):
        return self.get_form_classes()['document_form'](
            prefix=kwargs['prefix'],
            document=self.document,
            data=kwargs.get('data', None),
            files=kwargs.get('files', None),
        )

    def get_form_classes(self):
        return {'document_form': NewVersionForm, 'source_form': get_upload_form_class(self.source.source_type)}

    def get_context_data(self, **kwargs):
        context = super(UploadInteractiveVersionView, self).get_context_data(**kwargs)
        context['object'] = self.document
        context['title'] = _(u'Upload a new version from source: %s') % self.source.title

        return context


def staging_file_delete(request, staging_folder_pk, encoded_filename):
    Permission.objects.check_permissions(request.user, [PERMISSION_DOCUMENT_CREATE, PERMISSION_DOCUMENT_NEW_VERSION])
    staging_folder = get_object_or_404(StagingFolderSource, pk=staging_folder_pk)

    staging_file = staging_folder.get_file(encoded_filename=encoded_filename)
    next = request.POST.get('next', request.GET.get('next', request.META.get('HTTP_REFERER', reverse(settings.LOGIN_REDIRECT_URL))))
    previous = request.POST.get('previous', request.GET.get('previous', request.META.get('HTTP_REFERER', reverse(settings.LOGIN_REDIRECT_URL))))

    if request.method == 'POST':
        try:
            staging_file.delete()
            messages.success(request, _(u'Staging file delete successfully.'))
        except Exception as exception:
            messages.error(request, _(u'Staging file delete error; %s.') % exception)
        return HttpResponseRedirect(next)

    results = get_active_tab_links()

    return render_to_response('main/generic_confirm.html', {
        'source': staging_folder,
        'delete_view': True,
        'object': staging_file,
        'next': next,
        'previous': previous,
        'temporary_navigation_links': {'form_header': {'staging_file_delete': {'links': results['tab_links']}}},
    }, context_instance=RequestContext(request))


# Setup views
def setup_source_list(request):
    Permission.objects.check_permissions(request.user, [PERMISSION_SOURCES_SETUP_VIEW])

    context = {
        'object_list': Source.objects.select_subclasses(),
        'title': _('Sources'),
        'hide_link': True,
        'list_object_variable_name': 'source',
        'extra_columns': [
            {
                'name': _('Type'),
                'attribute': encapsulate(lambda x: x.class_fullname())
            },
        ]
    }

    return render_to_response('main/generic_list.html', context,
                              context_instance=RequestContext(request))


def setup_source_edit(request, source_id):
    Permission.objects.check_permissions(request.user, [PERMISSION_SOURCES_SETUP_EDIT])

    source = get_object_or_404(Source.objects.select_subclasses(), pk=source_id)
    form_class = get_form_class(source.source_type)

    next = request.POST.get('next', request.GET.get('next', request.META.get('HTTP_REFERER', reverse(settings.LOGIN_REDIRECT_URL))))

    if request.method == 'POST':
        form = form_class(instance=source, data=request.POST)
        if form.is_valid():
            try:
                form.save()
                messages.success(request, _(u'Source edited successfully'))
                return HttpResponseRedirect(next)
            except Exception as exception:
                messages.error(request, _(u'Error editing source; %s') % exception)
    else:
        form = form_class(instance=source)

    return render_to_response('main/generic_form.html', {
        'title': _(u'Edit source: %s') % source,
        'form': form,
        'source': source,
        'navigation_object_name': 'source',
        'next': next,
        'source_type': source.source_type,
    }, context_instance=RequestContext(request))


def setup_source_delete(request, source_id):
    Permission.objects.check_permissions(request.user, [PERMISSION_SOURCES_SETUP_DELETE])
    source = get_object_or_404(Source.objects.select_subclasses(), pk=source_id)
    redirect_view = reverse('sources:setup_source_list')

    previous = request.POST.get('previous', request.GET.get('previous', request.META.get('HTTP_REFERER', redirect_view)))

    if request.method == 'POST':
        try:
            source.delete()
            messages.success(request, _(u'Source "%s" deleted successfully.') % source)
        except Exception as exception:
            messages.error(request, _(u'Error deleting source "%(source)s": %(error)s') % {
                'source': source, 'error': exception
            })
        return HttpResponseRedirect(reverse(redirect_view))

    context = {
        'title': _(u'Are you sure you wish to delete the source: %s?') % source,
        'source': source,
        'navigation_object_name': 'source',
        'delete_view': True,
        'previous': previous,
        'source_type': source.source_type,
    }

    return render_to_response('main/generic_confirm.html', context,
                              context_instance=RequestContext(request))


def setup_source_create(request, source_type):
    Permission.objects.check_permissions(request.user, [PERMISSION_SOURCES_SETUP_CREATE])

    cls = get_class(source_type)
    form_class = get_form_class(source_type)

    if request.method == 'POST':
        form = form_class(data=request.POST)
        if form.is_valid():
            try:
                form.save()
                messages.success(request, _(u'Source created successfully'))
                return HttpResponseRedirect(reverse('sources:setup_source_list'))
            except Exception as exception:
                messages.error(request, _(u'Error creating source; %s') % exception)
    else:
        form = form_class()

    return render_to_response('main/generic_form.html', {
        'title': _(u'Create new source of type: %s') % cls.class_fullname(),
        'form': form,
        'source_type': source_type,
        'navigation_object_name': 'source',
    }, context_instance=RequestContext(request))


def setup_source_transformation_list(request, source_id):
    Permission.objects.check_permissions(request.user, [PERMISSION_SOURCES_SETUP_EDIT])

    source = get_object_or_404(Source.objects.select_subclasses(), pk=source_id)

    context = {
        'object_list': SourceTransformation.transformations.get_for_object(source),
        'title': _(u'Transformations for: %s') % source.fullname(),
        'source': source,
        'navigation_object_name': 'source',
        'list_object_variable_name': 'transformation',
        'extra_columns': [
            {'name': _(u'Order'), 'attribute': 'order'},
            {'name': _(u'Transformation'), 'attribute': encapsulate(lambda x: x.get_transformation_display())},
            {'name': _(u'Arguments'), 'attribute': 'arguments'}
        ],
        'hide_link': True,
        'hide_object': True,
    }

    return render_to_response('main/generic_list.html', context,
                              context_instance=RequestContext(request))


def setup_source_transformation_edit(request, transformation_id):
    Permission.objects.check_permissions(request.user, [PERMISSION_SOURCES_SETUP_EDIT])

    source_transformation = get_object_or_404(SourceTransformation, pk=transformation_id)
    redirect_view = reverse('sources:setup_source_transformation_list', args=[source_transformation.content_object.pk])
    next = request.POST.get('next', request.GET.get('next', request.META.get('HTTP_REFERER', redirect_view)))

    if request.method == 'POST':
        form = SourceTransformationForm(instance=source_transformation, data=request.POST)
        if form.is_valid():
            try:
                form.save()
                messages.success(request, _(u'Source transformation edited successfully'))
                return HttpResponseRedirect(next)
            except Exception as exception:
                messages.error(request, _(u'Error editing source transformation; %s') % exception)
    else:
        form = SourceTransformationForm(instance=source_transformation)

    return render_to_response('main/generic_form.html', {
        'title': _(u'Edit transformation: %s') % source_transformation,
        'form': form,
        'source': source_transformation.content_object,
        'transformation': source_transformation,
        'navigation_object_list': [
            {'object': 'source', 'name': _(u'Source')},
            {'object': 'transformation', 'name': _(u'Transformation')}
        ],
        'next': next,
    }, context_instance=RequestContext(request))


def setup_source_transformation_delete(request, transformation_id):
    Permission.objects.check_permissions(request.user, [PERMISSION_SOURCES_SETUP_EDIT])

    source_transformation = get_object_or_404(SourceTransformation, pk=transformation_id)
    redirect_view = reverse('sources:setup_source_transformation_list', args=[source_transformation.content_object.pk])
    previous = request.POST.get('previous', request.GET.get('previous', request.META.get('HTTP_REFERER', redirect_view)))

    if request.method == 'POST':
        try:
            source_transformation.delete()
            messages.success(request, _(u'Source transformation deleted successfully.'))
        except Exception as exception:
            messages.error(request, _(u'Error deleting source transformation; %(error)s') % {
                'error': exception}
            )
        return HttpResponseRedirect(redirect_view)

    return render_to_response('main/generic_confirm.html', {
        'delete_view': True,
        'transformation': source_transformation,
        'source': source_transformation.content_object,
        'navigation_object_list': [
            {'object': 'source', 'name': _(u'Source')},
            {'object': 'transformation', 'name': _(u'Transformation')}
        ],
        'title': _(u'Are you sure you wish to delete source transformation "%(transformation)s"') % {
            'transformation': source_transformation.get_transformation_display(),
        },
        'previous': previous,
    }, context_instance=RequestContext(request))


def setup_source_transformation_create(request, source_id):
    Permission.objects.check_permissions(request.user, [PERMISSION_SOURCES_SETUP_EDIT])

    source = get_object_or_404(Source.objects.select_subclasses(), pk=source_id)

    redirect_view = reverse('sources:setup_source_transformation_list', args=[source.pk])

    if request.method == 'POST':
        form = SourceTransformationForm_create(request.POST)
        if form.is_valid():
            try:
                source_tranformation = form.save(commit=False)
                source_tranformation.content_object = source
                source_tranformation.save()
                messages.success(request, _(u'Source transformation created successfully'))
                return HttpResponseRedirect(redirect_view)
            except Exception as exception:
                messages.error(request, _(u'Error creating source transformation; %s') % exception)
    else:
        form = SourceTransformationForm_create()

    return render_to_response('main/generic_form.html', {
        'form': form,
        'source': source,
        'navigation_object_name': 'source',
        'title': _(u'Create new transformation for source: %s') % source,
    }, context_instance=RequestContext(request))
