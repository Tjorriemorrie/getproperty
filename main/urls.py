from django.urls import path

from main import views

app_name = 'main'

urlpatterns = [
    path('', views.listing_list, name='listing_list'),
    path('listing/<str:listing_id>/', views.listing_detail, name='listing_detail'),
    path('mark-similar/', views.mark_similar, name='mark_similar'),
    path('listing/<str:listing_id>/unlink/', views.unlink_similar, name='unlink_similar'),
    path('listing/<str:listing_id>/set-primary/', views.set_primary, name='set_primary'),
    path('listing/<str:listing_id>/set-type/', views.set_property_type, name='set_property_type'),
    path('listing/<str:listing_id>/set-levy-placeholder/', views.set_levy_placeholder, name='set_levy_placeholder'),
    path('listing/<str:listing_id>/set-notes/', views.set_notes, name='set_notes'),
]
