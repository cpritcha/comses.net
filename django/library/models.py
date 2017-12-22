import logging
import mimetypes
import pathlib
import uuid

import os
import semver
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.postgres.aggregates import BoolOr, BoolAnd
from django.contrib.postgres.fields import JSONField, ArrayField
from django.core.files.images import ImageFile
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from enum import Enum
from model_utils import Choices
from modelcluster.contrib.taggit import ClusterTaggableManager
from modelcluster.fields import ParentalKey
from modelcluster.models import ClusterableModel
from rest_framework.exceptions import ValidationError
from taggit.models import TaggedItemBase
from unidecode import unidecode
from wagtail.wagtailimages.models import Image, AbstractImage, AbstractRendition, get_upload_to
from wagtail.wagtailsearch import index

from core import fs
from core.backends import get_viewable_objects_for_user
from core.fields import MarkdownField
from library.fs import CodebaseReleaseFsApi, StagingDirectories, FileCategoryDirectories, MessageLevels

logger = logging.getLogger(__name__)

# Cherry picked from
# https://www.ngdc.noaa.gov/metadata/published/xsd/schema/resources/Codelist/gmxCodelists.xml#CI_RoleCode
ROLES = Choices(
    ('author', _('Author')),
    ('publisher', _('Publisher')),
    ('custodian', _('Custodian')),
    ('resourceProvider', _('Resource Provider')),
    ('maintainer', _('Maintainer')),
    ('pointOfContact', _('Point of contact')),
    ('editor', _('Editor')),
    ('contributor', _('Contributor')),
    ('collaborator', _('Collaborator')),
    ('funder', _('Funder')),
    ('copyrightHolder', _("Copyright holder")),
)

OPERATING_SYSTEMS = Choices(
    ('other', _('Other')),
    ('linux', _('Unix/Linux')),
    ('macos', _('Mac OS')),
    ('windows', _('Windows')),
    ('platform_independent', _('Platform Independent')),
)


class CodebaseTag(TaggedItemBase):
    content_object = ParentalKey('library.Codebase', related_name='tagged_codebases')


class ProgrammingLanguage(TaggedItemBase):
    content_object = ParentalKey('library.CodebaseRelease', related_name='tagged_release_languages')


class CodebaseReleasePlatformTag(TaggedItemBase):
    content_object = ParentalKey('library.CodebaseRelease', related_name='tagged_release_platforms')


class ContributorAffiliation(TaggedItemBase):
    content_object = ParentalKey('library.Contributor')


class License(models.Model):
    name = models.CharField(max_length=200, help_text=_('SPDX license code from https://spdx.org/licenses/'))
    url = models.URLField(blank=True)


class Contributor(index.Indexed, ClusterableModel):
    given_name = models.CharField(max_length=100, blank=True,
                                  help_text=_('Also doubles as organizational name'))
    middle_name = models.CharField(max_length=100, blank=True)
    family_name = models.CharField(max_length=100, blank=True)
    affiliations = ClusterTaggableManager(through=ContributorAffiliation)
    type = models.CharField(max_length=16,
                            choices=(('person', 'person'), ('organization', 'organization')),
                            default='person',
                            help_text=_('organizations only use given_name'))
    email = models.EmailField(blank=True)
    user = models.ForeignKey(User, null=True)

    search_fields = [
        index.SearchField('given_name', partial_match=True, boost=10),
        index.SearchField('family_name', partial_match=True, boost=10),
        index.RelatedFields('affiliations', [
            index.SearchField('name', partial_match=True)
        ]),
        index.SearchField('email', partial_match=True),
        index.RelatedFields('user', [
            index.SearchField('first_name', partial_match=True, boost=10),
            index.SearchField('last_name', partial_match=True, boost=10),
        ]),
    ]

    @property
    def name(self):
        return self.get_full_name()

    def get_full_name(self, family_name_first=False):
        if self.type == 'person':
            # Bah
            if family_name_first:
                return '{0}, {1} {2}'.format(self.family_name, self.given_name, self.middle_name).strip()
            elif self.middle_name:
                return '{0} {1} {2}'.format(self.given_name, self.middle_name, self.family_name)
            else:
                return '{0} {1}'.format(self.given_name, self.family_name)
        else:
            return self.given_name

    @property
    def formatted_affiliations(self):
        return ' '.join(self.affiliations.all())

    def __str__(self):
        if self.email:
            return '{0} {1}'.format(self.get_full_name(), self.email)
        return self.get_full_name()


class SemanticVersionBump(Enum):
    MAJOR = semver.bump_major
    MINOR = semver.bump_minor
    PATCH = semver.bump_patch


class CodebaseReleaseDownload(models.Model):
    date_created = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(User, null=True)
    ip_address = models.GenericIPAddressField()
    referrer = models.URLField(max_length=500)
    release = models.ForeignKey('library.CodebaseRelease', related_name='downloads')

    def __str__(self):
        return "{0}: downloaded {1}".format(self.ip_address, self.release)


class CodebaseQuerySet(models.QuerySet):
    def with_liveness(self):
        """Add a live property to a codebase.

        A codebase is live if any of its releases are live. It is not live by default"""
        return self.annotate(live=Coalesce(BoolOr('releases__live'), False)) \
            .annotate(draft=Coalesce(BoolAnd('releases__draft'), True))

    def with_viewable_releases(self, user):
        queryset = get_viewable_objects_for_user(user=user,
                                                 queryset=CodebaseRelease.objects.all())
        return self.prefetch_related(
            models.Prefetch('releases', queryset=queryset))

    def accessible(self, user):
        return get_viewable_objects_for_user(user=user, queryset=self.with_viewable_releases(user=user).with_liveness())

    def contributed_by(self, user):
        contributed_codebases = ReleaseContributor.objects.filter(contributor__user=user) \
            .values_list('release__codebase', flat=True)
        # FIXME: consider replacing submitter with ReleaseContributor, see
        # https://github.com/comses/core.comses.net/issues/129 for more details
        return self.filter(
            models.Q(pk__in=contributed_codebases) | models.Q(submitter=user))

    def public(self):
        """Returns a queryset of all live codebases and their live releases"""
        return self.with_liveness().filter(live=True).filter(draft=False).prefetch_related(
            models.Prefetch('releases', queryset=CodebaseRelease.objects.public()))

    def peer_reviewed(self):
        return self.public().filter(peer_reviewed=True)


class Codebase(index.Indexed, ClusterableModel):
    """
    Metadata applicable across a set of CodebaseReleases
    """
    # shortname = models.CharField(max_length=128, unique=True)
    title = models.CharField(max_length=300)
    description = MarkdownField()
    summary = models.CharField(max_length=500, blank=True)

    featured = models.BooleanField(default=False)

    has_unpublished_changes = models.BooleanField(default=False)
    first_published_at = models.DateTimeField(null=True, blank=True)
    last_published_on = models.DateTimeField(null=True, blank=True)

    date_created = models.DateTimeField(default=timezone.now)
    last_modified = models.DateTimeField(auto_now=True)
    is_replication = models.BooleanField(default=False, help_text=_("Is this model a replication of another model?"))
    # FIXME: should this be a rollup of peer reviewed CodebaseReleases?
    peer_reviewed = models.BooleanField(default=False)

    # FIXME: right now leaning towards identifier as the agnostic way to ID any given Codebase. It is currently set to the
    # old Drupal NID but that means we need to come up with something on model upload
    identifier = models.CharField(max_length=128, unique=True)
    doi = models.CharField(max_length=128, unique=True, null=True)
    uuid = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)

    latest_version = models.ForeignKey('CodebaseRelease', null=True, related_name='latest_version')

    repository_url = models.URLField(blank=True,
                                     help_text=_('URL to code repository, e.g., https://github.com/comses/wolf-sheep'))
    # FIXME: original Drupal data was stored inline like this
    # after catalog integration remove these / replace with M2M relationships
    # publication metadata
    # We should also allow a model to have multiple references
    references_text = models.TextField(blank=True)
    associated_publication_text = models.TextField(blank=True)
    tags = ClusterTaggableManager(through=CodebaseTag)
    # evaluate this JSONField as an add-anything way to record relationships between this Codebase and other entities
    # with URLs / resolvable identifiers
    relationships = JSONField(default=list)

    # JSONField list of image metadata records with paths referring to self.media_dir()
    media = JSONField(default=list,
                      help_text=_("JSON metadata dict of media associated with this Codebase"))

    submitter = models.ForeignKey(User, related_name='codebases')

    objects = CodebaseQuerySet.as_manager()

    search_fields = [
        index.SearchField('title', partial_match=True, boost=10),
        index.SearchField('description', partial_match=True),
        index.SearchField('featured'),
        index.SearchField('first_published_at'),
        index.SearchField('last_published_on'),
        index.RelatedFields('tags', [
            index.SearchField('name'),
        ]),
    ]

    HAS_PUBLISHED_KEY = True

    @property
    def deletable(self):
        return not self.live

    @staticmethod
    def _release_upload_path(instance, filename):
        return str(pathlib.Path(instance.codebase.upload_path, filename))

    @staticmethod
    def assign_latest_release():
        for codebase in Codebase.objects.all():
            codebase.latest_version = codebase.releases.order_by('-version_number').first()
            codebase.save()

    def as_featured_content_dict(self):
        return dict(
            title=self.title,
            summary=self.summarized_description,
            codebase_image=self.get_featured_image(),
            link_codebase=self,
        )

    def get_featured_image(self):
        if self.featured_images.exists():
            return self.featured_images.first()
        return None

    def subpath(self, *args):
        return pathlib.Path(self.base_library_dir, *args)

    @property
    def upload_path(self):
        return self.media_dir('uploads')

    def media_dir(self, *args):
        return pathlib.Path(settings.LIBRARY_ROOT, str(self.uuid), 'media', *args)

    @property
    def summarized_description(self):
        if self.summary:
            return self.summary
        lines = self.description.raw.splitlines()
        max_lines = 6
        if len(lines) > max_lines:
            # FIXME: add a "more.." link, is this type of summarization more appropriate in JS?
            return "{0} \n...".format(
                "\n".join(lines[:max_lines])
            )
        return self.description.raw

    @property
    def base_library_dir(self):
        # FIXME: slice up UUID eventually if needed
        return pathlib.Path(settings.LIBRARY_ROOT, str(self.uuid))

    @property
    def base_git_dir(self):
        return pathlib.Path(settings.REPOSITORY_ROOT, str(self.uuid))

    @property
    def all_contributors(self):
        return ReleaseContributor.objects.select_related('release', 'contributor').filter(
            release__codebase__id=self.pk).distinct('contributor')

    @property
    def contributor_list(self):
        contributor_list = [c.contributor.get_full_name(family_name_first=True) for c in
                            self.all_contributors]
        return contributor_list

    def download_count(self):
        return CodebaseReleaseDownload.objects.filter(release__codebase__id=self.pk).count()

    @classmethod
    def get_list_url(cls):
        return reverse('library:codebase-list')

    def get_absolute_url(self):
        return reverse('library:codebase-detail', kwargs={'identifier': self.identifier})

    def get_draft_url(self):
        return reverse('library:codebaserelease-draft', kwargs={'identifier': self.identifier})

    def media_url(self, name):
        return '{0}/media/{1}'.format(self.get_absolute_url(), name)

    def next_version_number(self, version_number=None, version_bump=SemanticVersionBump.MINOR):
        if version_number is None:
            # start off at v1.0.0
            version_number = '1.0.0'
            # get the latest release's version_number
            # (can't use latest release because it is for published releases only)
            last_release = self.releases.last()
            if last_release is not None:
                version_number = version_bump(last_release.version_number)
        return version_number

    def import_release(self, submitter=None, submitter_id=None, version_number=None, submitted_package=None, **kwargs):
        if submitter_id is None:
            if submitter is None:
                submitter = User.objects.first()
                logger.warning("No submitter or submitter_id specified when creating release, using first user %s",
                               submitter)
            submitter_id = submitter.pk
        if version_number is None:
            version_number = self.next_version_number()

        identifier = kwargs.pop('identifier', None)
        if 'draft' not in kwargs:
            kwargs['draft'] = False
        if 'live' not in kwargs:
            kwargs['live'] = True
        release = CodebaseRelease.objects.create(
            submitter_id=submitter_id,
            version_number=version_number,
            identifier=identifier,
            codebase=self,
            **kwargs)
        if submitted_package:
            release.submitted_package.save(submitted_package.name, submitted_package, save=False)
        if release.is_published:
            self.latest_version = release
            self.save()
        return release

    def import_media(self, fileobj, title=None, media=None):
        if media is None:
            media = self.media
        name = os.path.basename(fileobj.name)
        path = self.media_dir(name)
        os.makedirs(str(path.parent), exist_ok=True)
        with path.open('wb') as f:
            f.write(fileobj.read())

        image_metadata = {
            'name': name,
            'path': str(self.media_dir()),
            'mimetype': mimetypes.guess_type(str(path)),
            'url': self.media_url(name),
            'featured': fs.is_image(str(path)),
        }

        logger.info('featured image: %s', image_metadata['name'])
        if image_metadata['featured']:
            filename = image_metadata['name']
            path = pathlib.Path(image_metadata['path'], filename)
            image = CodebaseImage(codebase=self,
                                  title=title or name or self.title,
                                  file=ImageFile(path.open('rb')),
                                  uploaded_by_user=self.submitter)
            image.save()
            self.featured_images.add(image)
            logger.info('added featured image')
            return image

        media.append(image_metadata)
        self.media = media
        return None

    def get_or_create_draft(self):
        draft = self.releases.filter(draft=True).first()
        if not draft:
            draft = self.create_release()
        return draft

    def create_release(self, initialize=True, **overrides):
        submitter = self.submitter
        version_number = self.next_version_number()
        previous_release = self.releases.last()
        kwargs = dict(submitter=submitter,
                      version_number=version_number,
                      identifier=None,
                      live=False,
                      draft=True)
        if previous_release is not None:
            previous_release_contributors = ReleaseContributor.objects.filter(release_id=previous_release.id)
            previous_release.id = None
            release = previous_release
            for k, v in kwargs.items():
                setattr(release, k, v)
            release.save()
            previous_release_contributors.copy_to(release)
        else:
            kwargs['codebase'] = self
            kwargs.update(overrides)
            release = CodebaseRelease.objects.create(**kwargs)
            contributor = self.submitter.contributor_set.get_or_create(defaults={
                'given_name': self.submitter.first_name,
                'family_name': self.submitter.last_name,
                'affiliations': [self.submitter.member_profile.institution.name] if hasattr(
                    self.submitter.member_profile.institution, 'name') else [],
                'email': self.submitter.email
            })[0]
            ReleaseContributor.objects.create(release=release,
                                              contributor=contributor,
                                              roles=['author'],
                                              index=0)

        if initialize:
            fs_api = release.get_fs_api()
            fs_api.initialize()
        if release.is_published:
            self.latest_version = release
            self.save()
        return release

    def __str__(self):
        live = repr(self.live) if hasattr(self, 'live') else 'Unknown'
        return "{0} {1} identifier={2} live={3}".format(self.title, self.date_created, repr(self.identifier),
                                                        live)

    class Meta:
        permissions = (('view_codebase', 'Can view codebase'),)


class CodebaseImage(AbstractImage):
    codebase = models.ForeignKey(Codebase, related_name='featured_images')
    file = models.ImageField(
        verbose_name=_('file'), upload_to=get_upload_to, width_field='width', height_field='height',
        storage=FileSystemStorage(location=settings.LIBRARY_ROOT)
    )

    admin_form_fields = Image.admin_form_fields + ('codebase',)

    def get_upload_to(self, filename):
        # adapted from wagtailimages/models
        folder_name = str(self.codebase.media_dir())
        filename = self.file.field.storage.get_valid_name(filename)

        # do a unidecode in the filename and then
        # replace non-ascii characters in filename with _ , to sidestep issues with filesystem encoding
        filename = "".join((i if ord(i) < 128 else '_') for i in unidecode(filename))

        # Truncate filename so it fits in the 100 character limit
        # https://code.djangoproject.com/ticket/9893
        full_path = os.path.join(folder_name, filename)
        if len(full_path) >= 95:
            chars_to_trim = len(full_path) - 94
            prefix, extension = os.path.splitext(filename)
            filename = prefix[:-chars_to_trim] + extension
            full_path = os.path.join(folder_name, filename)

        return full_path


class CodebaseRendition(AbstractRendition):
    image = models.ForeignKey(CodebaseImage, related_name='renditions', on_delete=models.CASCADE)

    class Meta:
        unique_together = (
            ('image', 'filter_spec', 'focal_point_key'),
        )


class CodebasePublication(models.Model):
    release = models.ForeignKey('library.CodebaseRelease', on_delete=models.CASCADE)
    publication = models.ForeignKey('citation.Publication', on_delete=models.CASCADE)
    is_primary = models.BooleanField(default=False)
    index = models.PositiveIntegerField(default=1)


class CodebaseReleaseQuerySet(models.QuerySet):
    def with_codebase(self, user):
        return self.prefetch_related(
            models.Prefetch('codebase__releases', queryset=CodebaseRelease.objects.accessible_without_codebase(user)))

    def public(self):
        return self.filter(draft=False).filter(live=True)

    def accessible_without_codebase(self, user):
        return get_viewable_objects_for_user(user, queryset=self)

    def accessible(self, user):
        return get_viewable_objects_for_user(user, queryset=self).with_codebase(user=user)


class CodebaseRelease(index.Indexed, ClusterableModel):
    """
    A snapshot of a codebase at a particular moment in time, versioned and addressable in a git repo behind-the-scenes
    and a bagit repository.

    Currently using simple FS organization in lieu of HashFS or other content addressable filesystem.

    * release tarballs or zipfiles located at /library/<codebase_identifier>/releases/<version_number>/<id>.(tar.gz|zip)
    * release bagits at /library/<codebase_identifier>/releases/<release_identifier>/sip
    * git repository in /repository/<codebase_identifier>/
    """

    date_created = models.DateTimeField(default=timezone.now)
    last_modified = models.DateTimeField(auto_now=True)

    live = models.BooleanField(default=False, help_text=_("Signifies that this release is public."))
    # there should only be one draft CodebaseRelease ever
    draft = models.BooleanField(default=False, help_text=_("Signifies that this release is currently being edited."))
    has_unpublished_changes = models.BooleanField(default=False)
    first_published_at = models.DateTimeField(null=True, blank=True)
    last_published_on = models.DateTimeField(null=True, blank=True)

    peer_reviewed = models.BooleanField(default=False)
    flagged = models.BooleanField(default=False)

    identifier = models.CharField(max_length=128, unique=True, null=True)
    doi = models.CharField(max_length=128, unique=True, null=True)
    license = models.ForeignKey(License, null=True)
    # FIXME: replace with or append/prepend README.md
    release_notes = MarkdownField(blank=True, help_text=_('Markdown formattable text, e.g., run conditions'))
    summary = models.CharField(max_length=500, blank=True)
    documentation = models.FileField(null=True, help_text=_('Fulltext documentation file (PDF/PDFA)'))
    embargo_end_date = models.DateTimeField(null=True, blank=True)
    version_number = models.CharField(max_length=32,
                                      help_text=_('semver string, e.g., 1.0.5, see semver.org'))

    os = models.CharField(max_length=32, choices=OPERATING_SYSTEMS, blank=True)
    dependencies = JSONField(
        default=list,
        help_text=_('JSON list of software dependencies (identifier, name, version, packageSystem, OS, URL)')
    )
    '''
    platform and programming language tags are also dependencies that can reference additional metadata in the
    dependencies JSONField
    '''
    platform_tags = ClusterTaggableManager(through=CodebaseReleasePlatformTag,
                                           related_name='platform_codebase_releases')
    platforms = models.ManyToManyField('core.Platform')
    programming_languages = ClusterTaggableManager(through=ProgrammingLanguage,
                                                   related_name='pl_codebase_releases')
    codebase = models.ForeignKey(Codebase, related_name='releases')
    submitter = models.ForeignKey(User)
    contributors = models.ManyToManyField(Contributor, through='ReleaseContributor')
    submitted_package = models.FileField(upload_to=Codebase._release_upload_path, null=True)
    # M2M relationships for publications
    publications = models.ManyToManyField(
        'citation.Publication',
        through=CodebasePublication,

        related_name='releases',
        help_text=_('Publications on this work'))
    references = models.ManyToManyField('citation.Publication',
                                        related_name='codebase_references',
                                        help_text=_('Related publications'))

    objects = CodebaseReleaseQuerySet.as_manager()

    def get_edit_url(self):
        return reverse('library:codebaserelease-edit', kwargs={'identifier': self.codebase.identifier,
                                                               'version_number': self.version_number})

    def get_list_url(self):
        return reverse('library:codebaserelease-list', kwargs={'identifier': self.codebase.identifier})

    def get_absolute_url(self):
        return reverse('library:codebaserelease-detail',
                       kwargs={'identifier': self.codebase.identifier, 'version_number': self.version_number})

    # FIXME: lift magic constants
    @property
    def handle_url(self):
        if '2286.0/oabm' in self.doi:
            return 'http://hdl.handle.net/{0}'.format(self.doi)
        return None

    @property
    def doi_url(self):
        return 'http://dx.doi.org/{0}'.format(self.doi)

    @property
    def permanent_url(self):
        if self.doi:
            if '2286.0/oabm' in self.doi:
                return self.handle_url
            else:
                return self.doi_url
        return 'https://www.comses.net{0}'.format(self.get_absolute_url())

    @property
    def citation_text(self):
        authors = ', '.join(self.contributor_list)
        return '{authors} ({publish_date}). "{title}" (Version {version}). _{cml}_. Retrieved from: {purl}'.format(
            authors=authors,
            publish_date=self.last_published_on.strftime('%Y, %B %d'),
            title=self.codebase.title,
            version=self.version_number,
            cml='CoMSES Computational Model Library',
            purl=self.permanent_url
        ) if self.last_published_on else 'You must publish this model in order to cite it'

    def download_count(self):
        return self.downloads.count()

    @property
    def contributor_list(self):
        return [c.contributor.get_full_name(family_name_first=True) for c in
                self.codebase_contributors.order_by('index')]

    @property
    def index_ordered_release_contributors(self):
        return self.codebase_contributors.order_by('index')

    @property
    def bagit_info(self):
        return {
            'Contact-Name': self.submitter.get_full_name(),
            'Contact-Email': self.submitter.email,
            'Author': self.codebase.contributor_list,
            'Version-Number': self.version_number,
            'Codebase-DOI': str(self.codebase.doi),
            'DOI': str(self.doi),
            # FIXME: check codemeta for additional metadata
        }

    @property
    def is_published(self):
        return self.live and not self.draft

    def get_fs_api(self, raise_exception_level=MessageLevels.warning) -> CodebaseReleaseFsApi:
        return CodebaseReleaseFsApi(uuid=self.codebase.uuid, identifier=self.codebase.identifier,
                                    version_number=self.version_number, raise_exception_level=raise_exception_level)

    def publish(self):
        publisher = CodebaseReleasePublisher(self)
        publisher.publish()

    def __str__(self):
        return '{0} {1} v{2}'.format(self.codebase, self.submitter.username, self.version_number)

    class Meta:
        unique_together = ('codebase', 'version_number')
        permissions = (('view_codebaserelease', 'Can view codebase release'),)


class CodebaseReleasePublisher:
    def __init__(self, codebase_release: CodebaseRelease):
        self.codebase_release = codebase_release

    def is_publishable(self):
        if not self.codebase_release.contributors.filter(user=self.codebase_release.submitter).exists():
            raise ValidationError('Submitter must be in the contributor list')
        fs_api = self.codebase_release.get_fs_api()
        storage = fs_api.get_stage_storage(StagingDirectories.sip)
        category = FileCategoryDirectories.code
        if storage.exists(category.name):
            files = list(storage.list(category))
        else:
            files = []
        if not files:
            raise ValidationError('Must have at least one source file')

    def publish(self):
        if not self.codebase_release.live:
            self.is_publishable()
            now = timezone.now()
            self.codebase_release.first_published_at = now
            self.codebase_release.last_published_on = now
            self.codebase_release.live = True
            self.codebase_release.draft = False
            fs_api = self.codebase_release.get_fs_api()
            fs_api.get_or_create_sip_bag(self.codebase_release.bagit_info)
            fs_api.build_aip()
            fs_api.build_archive()
            self.codebase_release.save()

            self.codebase_release.codebase.latest_version = self.codebase_release
            self.codebase_release.codebase.save()


class ReleaseContributorQuerySet(models.QuerySet):
    def copy_to(self, release: CodebaseRelease):
        release_contributors = list(self)
        for release_contributor in release_contributors:
            release_contributor.id = None
            release_contributor.release = release

        return self.bulk_create(release_contributors)


class ReleaseContributor(models.Model):
    release = models.ForeignKey(CodebaseRelease, on_delete=models.CASCADE, related_name='codebase_contributors')
    contributor = models.ForeignKey(Contributor, on_delete=models.CASCADE, related_name='codebase_contributors')
    include_in_citation = models.BooleanField(default=True)
    roles = ArrayField(models.CharField(
        max_length=100, choices=ROLES, default=ROLES.author,
        help_text=_(
            'Roles from https://www.ngdc.noaa.gov/metadata/published/xsd/schema/resources/Codelist/gmxCodelists.xml#CI_RoleCode'
        )
    ), default=list)
    index = models.PositiveSmallIntegerField(help_text=_('Ordering field for codebase contributors'))

    objects = ReleaseContributorQuerySet.as_manager()

    def __str__(self):
        return "{0} contributor {1}".format(self.release, self.contributor)
