from wagtail.wagtailsearch import index
from wagtail.wagtailsearch.index import FilterField, SearchField, RelatedFields, Indexed


class PrefixOptionalFilterField(FilterField):
    def __init__(self, field_name, should_prefix=True, **kwargs):
        super().__init__(field_name, **kwargs)
        self.should_prefix = should_prefix
