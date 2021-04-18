""" book list views"""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q, Max
from django.db.models.functions import Coalesce
from django.http import HttpResponseNotFound, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_POST

from bookwyrm import forms, models
from bookwyrm.activitypub import ActivitypubResponse
from bookwyrm.connectors import connector_manager
from .helpers import is_api_request, privacy_filter
from .helpers import get_user_from_username


# pylint: disable=no-self-use
class Lists(View):
    """ book list page """

    def get(self, request):
        """ display a book list """
        try:
            page = int(request.GET.get("page", 1))
        except ValueError:
            page = 1

        # hide lists with no approved books
        lists = (
            models.List.objects.annotate(
                item_count=Count("listitem", filter=Q(listitem__approved=True))
            )
            .filter(item_count__gt=0)
            .order_by("-updated_date")
            .distinct()
        )

        lists = privacy_filter(
            request.user, lists, privacy_levels=["public", "followers"]
        )

        paginated = Paginator(lists, 12)
        data = {
            "lists": paginated.get_page(page),
            "list_form": forms.ListForm(),
            "path": "/list",
        }
        return TemplateResponse(request, "lists/lists.html", data)

    @method_decorator(login_required, name="dispatch")
    # pylint: disable=unused-argument
    def post(self, request):
        """ create a book_list """
        form = forms.ListForm(request.POST)
        if not form.is_valid():
            return redirect("lists")
        book_list = form.save()

        return redirect(book_list.local_path)


class UserLists(View):
    """ a user's book list page """

    def get(self, request, username):
        """ display a book list """
        try:
            page = int(request.GET.get("page", 1))
        except ValueError:
            page = 1
        user = get_user_from_username(request.user, username)
        lists = models.List.objects.filter(user=user).all()
        lists = privacy_filter(request.user, lists)
        paginated = Paginator(lists, 12)

        data = {
            "user": user,
            "is_self": request.user.id == user.id,
            "lists": paginated.get_page(page),
            "list_form": forms.ListForm(),
            "path": user.local_path + "/lists",
        }
        return TemplateResponse(request, "user/lists.html", data)


class List(View):
    """ book list page """

    def get(self, request, list_id):
        """ display a book list """
        book_list = get_object_or_404(models.List, id=list_id)
        if not book_list.visible_to_user(request.user):
            return HttpResponseNotFound()

        if is_api_request(request):
            return ActivitypubResponse(book_list.to_activity(**request.GET))

        query = request.GET.get("q")
        suggestions = None

        # sort_by shall be "order" unless a valid alternative is given
        sort_by = request.GET.get("sort_by", "order")
        if sort_by not in ("order", "title", "rating"):
            sort_by = "order"

        # direction shall be "ascending" unless a valid alternative is given
        direction = request.GET.get("direction", "ascending")
        if direction not in ("ascending", "descending"):
            direction = "ascending"

        page = request.GET.get("page", 1)

        internal_sort_by = {
            "order": "order",
            "title": "book__title",
            "rating": "average_rating",
        }
        directional_sort_by = internal_sort_by[sort_by]
        if direction == "descending":
            directional_sort_by = "-" + directional_sort_by

        if sort_by == "order":
            items = book_list.listitem_set.filter(approved=True).order_by(
                directional_sort_by
            )
        elif sort_by == "title":
            items = book_list.listitem_set.filter(approved=True).order_by(
                directional_sort_by
            )
        elif sort_by == "rating":
            items = (
                book_list.listitem_set.annotate(
                    average_rating=Avg(Coalesce("book__review__rating", 0))
                )
                .filter(approved=True)
                .order_by(directional_sort_by)
            )

        paginated = Paginator(items, 25)

        if query and request.user.is_authenticated:
            # search for books
            suggestions = connector_manager.local_search(query, raw=True)
        elif request.user.is_authenticated:
            # just suggest whatever books are nearby
            suggestions = request.user.shelfbook_set.filter(
                ~Q(book__in=book_list.books.all())
            )
            suggestions = [s.book for s in suggestions[:5]]
            if len(suggestions) < 5:
                suggestions += [
                    s.default_edition
                    for s in models.Work.objects.filter(
                        ~Q(editions__in=book_list.books.all()),
                    ).order_by("-updated_date")
                ][: 5 - len(suggestions)]

        data = {
            "list": book_list,
            "items": paginated.get_page(page),
            "pending_count": book_list.listitem_set.filter(approved=False).count(),
            "suggested_books": suggestions,
            "list_form": forms.ListForm(instance=book_list),
            "query": query or "",
            "sort_form": forms.SortListForm(
                {"direction": direction, "sort_by": sort_by}
            ),
        }
        return TemplateResponse(request, "lists/list.html", data)

    @method_decorator(login_required, name="dispatch")
    # pylint: disable=unused-argument
    def post(self, request, list_id):
        """ edit a list """
        book_list = get_object_or_404(models.List, id=list_id)
        form = forms.ListForm(request.POST, instance=book_list)
        if not form.is_valid():
            return redirect("list", book_list.id)
        book_list = form.save()
        return redirect(book_list.local_path)


class Curate(View):
    """ approve or discard list suggestsions """

    @method_decorator(login_required, name="dispatch")
    def get(self, request, list_id):
        """ display a pending list """
        book_list = get_object_or_404(models.List, id=list_id)
        if not book_list.user == request.user:
            # only the creater can curate the list
            return HttpResponseNotFound()

        data = {
            "list": book_list,
            "pending": book_list.listitem_set.filter(approved=False),
            "list_form": forms.ListForm(instance=book_list),
        }
        return TemplateResponse(request, "lists/curate.html", data)

    @method_decorator(login_required, name="dispatch")
    # pylint: disable=unused-argument
    def post(self, request, list_id):
        """ edit a book_list """
        book_list = get_object_or_404(models.List, id=list_id)
        suggestion = get_object_or_404(models.ListItem, id=request.POST.get("item"))
        approved = request.POST.get("approved") == "true"
        if approved:
            suggestion.approved = True
            suggestion.save()
        else:
            deleted_order = suggestion.order
            suggestion.delete(broadcast=False)
            normalize_book_list_ordering(book_list.id, start=deleted_order)
        return redirect("list-curate", book_list.id)


@require_POST
def add_book(request):
    """ put a book on a list """
    book_list = get_object_or_404(models.List, id=request.POST.get("list"))
    if not book_list.visible_to_user(request.user):
        return HttpResponseNotFound()

    order_max = book_list.listitem_set.aggregate(Max("order"))["order__max"] or 0

    book = get_object_or_404(models.Edition, id=request.POST.get("book"))
    # do you have permission to add to the list?
    try:
        if request.user == book_list.user or book_list.curation == "open":
            # go ahead and add it
            models.ListItem.objects.create(
                book=book,
                book_list=book_list,
                user=request.user,
                order=order_max + 1,
            )
        elif book_list.curation == "curated":
            # make a pending entry
            models.ListItem.objects.create(
                approved=False,
                book=book,
                book_list=book_list,
                user=request.user,
                order=order_max + 1,
            )
        else:
            # you can't add to this list, what were you THINKING
            return HttpResponseBadRequest()
    except IntegrityError:
        # if the book is already on the list, don't flip out
        pass

    return redirect("list", book_list.id)


@require_POST
def remove_book(request, list_id):
    """ remove a book from a list """
    with transaction.atomic():
        book_list = get_object_or_404(models.List, id=list_id)
        item = get_object_or_404(models.ListItem, id=request.POST.get("item"))

        if not book_list.user == request.user and not item.user == request.user:
            return HttpResponseNotFound()

        deleted_order = item.order
        item.delete()
    normalize_book_list_ordering(book_list.id, start=deleted_order)
    return redirect("list", list_id)


@require_POST
def set_book_position(request, list_item_id):
    """
    Action for when the list user manually specifies a list position, takes special care with the unique ordering per list
    """
    with transaction.atomic():
        list_item = get_object_or_404(models.ListItem, id=list_item_id)
        try:
            int_position = int(request.POST.get("position"))
        except ValueError:
            return HttpResponseBadRequest(
                "bad value for position. should be an integer"
            )

        if int_position < 1:
            return HttpResponseBadRequest("position cannot be less than 1")

        book_list = list_item.book_list
        order_max = book_list.listitem_set.aggregate(Max("order"))["order__max"]

        if int_position > order_max:
            int_position = order_max

        if request.user not in (book_list.user, list_item.user):
            return HttpResponseNotFound()

        original_order = list_item.order
        if original_order == int_position:
            return
        elif original_order > int_position:
            list_item.order = -1
            list_item.save()
            increment_order_in_reverse(book_list.id, int_position, original_order)
        else:
            list_item.order = -1
            list_item.save()
            decrement_order(book_list.id, original_order, int_position)

        list_item.order = int_position
        list_item.save()

    return redirect("list", book_list.id)


@transaction.atomic
def increment_order_in_reverse(book_list_id, start, end):
    try:
        book_list = models.List.objects.get(id=book_list_id)
    except models.List.DoesNotExist:
        return
    items = book_list.listitem_set.filter(order__gte=start, order__lt=end).order_by(
        "-order"
    )
    for item in items:
        item.order += 1
        item.save()


@transaction.atomic
def decrement_order(book_list_id, start, end):
    try:
        book_list = models.List.objects.get(id=book_list_id)
    except models.List.DoesNotExist:
        return
    items = book_list.listitem_set.filter(order__gt=start, order__lte=end).order_by(
        "order"
    )
    for item in items:
        item.order -= 1
        item.save()


@transaction.atomic
def normalize_book_list_ordering(book_list_id, start=0, add_offset=0):
    try:
        book_list = models.List.objects.get(id=book_list_id)
    except models.List.DoesNotExist:
        return
    items = book_list.listitem_set.filter(order__gt=start).order_by("order")
    for i, item in enumerate(items, start):
        effective_order = i + add_offset
        if item.order != effective_order:
            item.order = effective_order
            item.save()
