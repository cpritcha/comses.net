from wagtail.wagtailsearch.backends import elasticsearch5
from wagtail.wagtailsearch.backends.elasticsearch2 import get_model_root

from .index import FilterField, SearchField, RelatedFields, PrefixOptionalFilterField


class Elasticsearch5Mapping(elasticsearch5.Elasticsearch5Mapping):
    def get_field_column_name(self, field):
        # Fields in derived models get prefixed with their model name, fields
        # in the root model don't get prefixed at all
        # This is to prevent mapping clashes in cases where two page types have
        # a field with the same name but a different type.
        root_model = get_model_root(self.model)
        definition_model = field.get_definition_model(self.model)

        if definition_model != root_model and getattr(field, 'should_prefix', None):
            prefix = definition_model._meta.app_label.lower() + '_' + definition_model.__name__.lower() + '__'
        else:
            prefix = ''

        if isinstance(field, (FilterField, PrefixOptionalFilterField)):
            return prefix + field.get_attname(self.model) + '_filter'
        elif isinstance(field, SearchField):
            return prefix + field.get_attname(self.model)
        elif isinstance(field, RelatedFields):
            return prefix + field.field_name


class ElasticSearch5SearchBackend(elasticsearch5.Elasticsearch5SearchBackend):
    mapping_class = Elasticsearch5Mapping


SearchBackend = ElasticSearch5SearchBackend
