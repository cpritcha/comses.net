from django.conf import settings
from django.urls import path, re_path
from rest_framework.routers import SimpleRouter

from core.settings.defaults import Environment
from . import views

app_name = 'library'

router = SimpleRouter()
router.register(r'codebases', views.CodebaseViewSet)
router.register(r'codebases/(?P<identifier>[\w\-.]+)/media', views.CodebaseFilesViewSet)
router.register(r'codebases/(?P<identifier>[\w\-.]+)/releases', views.CodebaseReleaseViewSet)
router.register(r'reviews/(?P<slug>[\da-f\-]+)/editor/invitations', views.PeerReviewInvitationViewSet),
router.register(r'reviews/(?P<slug>[\da-f\-]+)/editor/feedback', views.PeerReviewFeedbackViewSet),
router.register(views.CodebaseReleaseFilesSipViewSet.get_url_matcher(),
                views.CodebaseReleaseFilesSipViewSet, base_name='codebaserelease-sip-files')
router.register(views.CodebaseReleaseFilesOriginalsViewSet.get_url_matcher(),
                views.CodebaseReleaseFilesOriginalsViewSet, base_name='codebaserelease-original-files')
router.register(r'codebase-release', views.CodebaseReleaseShareViewSet, base_name='codebaserelease-share')

if settings.DEPLOY_ENVIRONMENT == Environment.DEVELOPMENT:
    router.register(r'test_codebases', views.DevelopmentCodebaseDeleteView, base_name='test_codebases')

urlpatterns = [
    path('reviews/dashboard/', views.PeerReviewDashboardView.as_view(), name='peer-review-dashboard'),
    path('reviews/<uuid:slug>/editor/', views.PeerReviewEditorView.as_view(), name='peer-review-detail'),
    path('invitation/<uuid:slug>/',
         views.PeerReviewInvitationUpdateView.as_view(), name='peer-review-invitation'),
    path('invitation/<uuid:slug>/feedback/',
         views.PeerReviewFeedbackListView.as_view(), name='peer-review-feedback-list'),
    path('invitation/<uuid:slug>/feedback/edit/',
         views.PeerReviewFeedbackUpdateView.as_view(), name='peer-review-feedback-edit'),

    path('contributors/', views.ContributorList.as_view()),
    path('codebases/add/', views.CodebaseFormCreateView.as_view(), name='codebase-add'),
    path('codebases/<slug:identifier>/edit/', views.CodebaseFormUpdateView.as_view(),
         name='codebase-edit'),
    path('codebases/<slug:identifier>/releases/draft/', views.CodebaseReleaseDraftView.as_view(),
         name='codebaserelease-draft'),
    path('codebases/<slug:identifier>/version/<int:version_number>/',
         views.CodebaseVersionRedirectView.as_view(), name='version-redirect'),
    re_path(r'^codebases/(?P<identifier>[\w\-.]+)/releases/(?P<version_number>\d+\.\d+\.\d+)/peer_review/$',
            views.create_peer_review, name='codebaserelease-request-peer-review'),
    re_path(r'^codebases/(?P<identifier>[\w\-.]+)/releases/(?P<version_number>\d+\.\d+\.\d+)/edit/$',
            views.CodebaseReleaseFormUpdateView.as_view(), name='codebaserelease-edit'),
    path('codebases/<slug:identifier>/releases/add/',
         views.CodebaseReleaseFormCreateView.as_view(), name='codebaserelease-add'),
] + router.urls