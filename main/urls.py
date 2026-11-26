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
    path('listing/<str:listing_id>/set-is-complex/', views.set_is_complex, name='set_is_complex'),
    path('listing/<str:listing_id>/set-complex/', views.set_complex_name, name='set_complex_name'),
    path('listing/<str:listing_id>/set-notes/', views.set_notes, name='set_notes'),
    path('listing/<str:listing_id>/set-liked/', views.set_liked, name='set_liked'),
    path('listing/<str:listing_id>/set-main-image/', views.set_main_image, name='set_main_image'),
    path('listing/<str:listing_id>/delete/', views.delete_listing, name='delete_listing'),
]
