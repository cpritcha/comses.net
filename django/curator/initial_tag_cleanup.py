from django.db import transaction

from .models import PendingTagCleanup, TagProxy

acronyms = [
    ('cellular automata', 'ca'),
    ('complex adaptive system', 'cas'),
    ('genetic algorithm', 'ga'),
    ('belief desire intention', 'bdi')
]


@transaction.atomic
def load_initial_data():
    for (r, l) in acronyms:
        PendingTagCleanup.objects.create(new_name=r, old_name=l)
    PendingTagCleanup.objects.process()

    PendingTagCleanup.objects.bulk_create(PendingTagCleanup.find_groups_by_porter_stemmer())
    bad_translations = [
        ('dynamic systems', 'system dynamics'),
        ('effect size', 'size effect'),
        ('flood re', 'flooding'),
        ('from', 'other')
    ]
    for bad_translation in bad_translations:
        PendingTagCleanup.objects.get(new_name=bad_translation[0], old_name=bad_translation[1]).delete()
    PendingTagCleanup.objects.process()

    PendingTagCleanup.objects.bulk_create(PendingTagCleanup.find_groups_by_platform_and_language())
    PendingTagCleanup.objects.process()

    # Ad Hoc Deletions
    regexes = [r'^jdk', r'^(?:ms|microsoft v)', r'^\.net', r'^version', 'r^visual s', 'r^jbuilder', r'^\d+\.',
               r'^length>', r'^from$', r'^other$']
    for regex in regexes:
        PendingTagCleanup.objects.bulk_create(TagProxy.objects.filter(name__iregex=regex).to_tag_cleanups())

    # Couldn't figure out what this abm platform is
    PendingTagCleanup.objects.create(new_name='LPL', old_name='LPL 5.55')

    PendingTagCleanup.objects.process()
