from __future__ import absolute_import

from django import forms
from django.core.urlresolvers import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext
from django.utils.translation import ugettext_lazy as _

from common.classes import ModelAttribute
from documents.models import Document
from documents.widgets import document_html_widget
from documents.settings import MULTIPAGE_PREVIEW_SIZE

from .models import SmartLink, SmartLinkCondition


class SmartLinkForm(forms.ModelForm):
    class Meta:
        fields = ('title', 'dynamic_title', 'enabled')
        model = SmartLink


class SmartLinkConditionForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        smart_link = kwargs.pop('smart_link', None)
        super(SmartLinkConditionForm, self).__init__(*args, **kwargs)
        self.fields['foreign_document_data'] = forms.ChoiceField(choices=ModelAttribute.get_choices_for(Document, type_names=['field', 'query']), label=_('Foreign document attribute'))
        self.fields['expression'].help_text = ' '.join([self.fields['expression'].help_text, ModelAttribute.help_text_for(Document, type_names=['field', 'related', 'property'])])

    class Meta:
        model = SmartLinkCondition
        exclude = ('smart_link',)


class SmartLinkImageWidget(forms.widgets.Widget):
    def render(self, name, value, attrs=None):
        output = []
        # TODO: convert to navigation app
        if value['links']:
            output.append(u'<div class="group navform wat-cf">')
            for link in value['links']:
                output.append(u'''
                    <button class="button" type="submit" name="action" value="%(action)s">
                        <span class="famfam active famfam-%(famfam)s"></span>%(text)s
                    </button>
                ''' % {
                    'famfam': link.get('famfam', u'link'),
                    'text': link['text'],
                    'action': reverse(link.get('view'), args=[value['current_document'].pk, value['smart_link_instance'].pk])
                })
            output.append(u'</div>')

        # TODO: Move CSS markup outside of forms.py
        output.append(u'<div style="white-space:nowrap; overflow: auto;">')
        for document in value['documents']:
            output.append(u'<div style="display: inline-block; margin: 0px 10px 10px 10px; %s">' % (u'border: 5px solid black; padding: 3px;' if value['current_document'] == document else u''))
            output.append(u'<div class="tc">%s</div>' % document)
            output.append(u'<div class="tc">%s: %d</div>' % (ugettext(u'Pages'), document.pages.count()))
            output.append(u'<div style="padding: 5px;">')
            output.append(document_html_widget(document, click_view='documents:document_display', size=MULTIPAGE_PREVIEW_SIZE, fancybox_class='fancybox-noscaling', gallery_name=u'smart_link_%d_documents_gallery' % value['smart_link_instance'].pk))
            output.append(u'</div>')
            output.append(u'<div class="tc">')
            output.append(u'<a href="%s"><span class="famfam active famfam-page_go"></span>%s</a>' % (reverse('documents:document_view_simple', args=[document.pk]), ugettext(u'Select')))
            output.append(u'</div>')
            output.append(u'</div>')

        output.append(u'</div>')
        output.append(
            u'<br /><span class="famfam active famfam-magnifier"></span>%s' %
            ugettext(u'Click on the image for full size view of the first page.')
        )

        return mark_safe(u''.join(output))


class SmartLinkInstanceForm(forms.Form):
    def __init__(self, *args, **kwargs):
        smart_link_instances = kwargs.pop('smart_link_instances', None)
        links = kwargs.pop('links', None)
        current_document = kwargs.pop('current_document', None)

        super(SmartLinkInstanceForm, self).__init__(*args, **kwargs)

        for smart_link_instance, data in smart_link_instances.items():
            self.fields['preview-%s' % smart_link_instance.pk] = forms.CharField(
                widget=SmartLinkImageWidget(),
                label=u'%s (%d)' % (unicode(data['title']), len(data['documents'])),
                required=False,
                initial={
                    'smart_link_instance': smart_link_instance,
                    'documents': data['documents'],
                    'current_document': current_document,
                    'links': links
                }
            )
