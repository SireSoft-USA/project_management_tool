from django import forms


class UserComboboxWidget(forms.Select):
    pass


class UserMultiComboboxWidget(forms.SelectMultiple):
    """Marker widget selecting the multi-select combobox template.

    Carries no behaviour of its own. Django derives ``widget_type`` from the
    class name, and apps.utils.templatetags.forms_tags dispatches on that to
    pick utils/forms/field_usermulticombobox.html - which is what renders the
    Alpine.js chips-and-search control instead of a bare <select multiple>.
    """
