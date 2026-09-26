from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from .forms import GardenForm, TroughForm, WitherBatchForm
from .models import Garden, Trough, WitherBatch


def _wants_htmx(request):
    return request.headers.get("HX-Request") == "true"


@login_required
def home(request):
    context = {
        "garden_count": Garden.objects.count(),
        "trough_count": Trough.objects.count(),
        "batch_count": WitherBatch.objects.count(),
        # 只计状态已是「可下槽」的槽；与萎凋槽列表 ?status=ready 同走 QuerySet.ready()。
        "ready_count": Trough.objects.ready().count(),
        "withering_count": Trough.objects.filter(
            status=Trough.STATUS_WITHERING
        ).count(),
        "loading_count": Trough.objects.filter(
            status=Trough.STATUS_LOADING
        ).count(),
    }
    return render(request, "home.html", context)


# ---- Garden ----


class GardenListView(LoginRequiredMixin, ListView):
    model = Garden
    template_name = "gardens/list.html"
    context_object_name = "gardens"

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "gardens/_table.html",
                {"gardens": self.object_list},
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class GardenCreateView(LoginRequiredMixin, CreateView):
    model = Garden
    form_class = GardenForm
    template_name = "gardens/form.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已创建")
        response = super().form_valid(form)
        if _wants_htmx(self.request):
            return redirect("garden_list")
        return response


class GardenUpdateView(LoginRequiredMixin, UpdateView):
    model = Garden
    form_class = GardenForm
    template_name = "gardens/form.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已更新")
        return super().form_valid(form)


class GardenDeleteView(LoginRequiredMixin, DeleteView):
    model = Garden
    template_name = "gardens/confirm_delete.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已删除")
        return super().form_valid(form)


# ---- Trough ----


# 列表状态筛选白名单；"ready" 走与首页计数同一个 QuerySet.ready() 口径。
_TROUGH_STATUS_FILTERS = {
    Trough.STATUS_READY,
    Trough.STATUS_WITHERING,
    Trough.STATUS_LOADING,
}


class TroughListView(LoginRequiredMixin, ListView):
    model = Trough
    template_name = "troughs/list.html"
    context_object_name = "troughs"

    def get_queryset(self):
        qs = Trough.objects.select_related("garden")
        status = self.request.GET.get("status", "")
        if status == Trough.STATUS_READY:
            qs = qs.ready()
        elif status in _TROUGH_STATUS_FILTERS:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        status = self.request.GET.get("status", "")
        ctx["active_status"] = status if status in _TROUGH_STATUS_FILTERS else ""
        ctx["ready_count"] = Trough.objects.ready().count()
        ctx["withering_count"] = Trough.objects.filter(
            status=Trough.STATUS_WITHERING
        ).count()
        ctx["loading_count"] = Trough.objects.filter(
            status=Trough.STATUS_LOADING
        ).count()
        return ctx

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "troughs/_table.html",
                {"troughs": self.object_list},
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class TroughDetailView(LoginRequiredMixin, DetailView):
    model = Trough
    template_name = "troughs/detail.html"
    context_object_name = "trough"

    def get_queryset(self):
        return Trough.objects.select_related("garden")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        trough = self.object
        # 详情、改态入口（编辑页）、模型 clean 全部读这两个同源方法。
        ctx["latest_batch"] = trough.latest_batch()
        ctx["latest_moisture"] = trough.latest_actual_moisture()
        ctx["ready_reason"] = trough.ready_block_reason()
        return ctx


class TroughCreateView(LoginRequiredMixin, CreateView):
    model = Trough
    form_class = TroughForm
    template_name = "troughs/form.html"
    success_url = reverse_lazy("trough_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已创建")
        return super().form_valid(form)


class TroughUpdateView(LoginRequiredMixin, UpdateView):
    model = Trough
    form_class = TroughForm
    template_name = "troughs/form.html"
    success_url = reverse_lazy("trough_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        trough = self.object
        # 改态入口展示的最新实测/资格提示，与详情页、模型校验同源。
        ctx["latest_batch"] = trough.latest_batch()
        ctx["latest_moisture"] = trough.latest_actual_moisture()
        ctx["ready_reason"] = trough.ready_block_reason()
        return ctx

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已更新")
        return super().form_valid(form)


class TroughDeleteView(LoginRequiredMixin, DeleteView):
    model = Trough
    template_name = "troughs/confirm_delete.html"
    success_url = reverse_lazy("trough_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已删除")
        return super().form_valid(form)


# ---- WitherBatch ----


def _announce_eligibility(request, trough_id, was_ready):
    """批次写完/删除后的中文提示。状态退回本身由模型层 enforce 兜底。"""
    trough = Trough.objects.get(pk=trough_id)
    reason = trough.ready_block_reason()
    if was_ready and trough.status != Trough.STATUS_READY and reason:
        messages.warning(
            request,
            f"{trough} 的最新批次已变化（{reason}）状态已退回「萎凋中」。",
        )
    elif trough.status != Trough.STATUS_READY and reason is None:
        messages.success(
            request,
            f"最新批次实测含水率 {trough.latest_actual_moisture()}%，"
            "该槽已具备「可下槽」资格，可在槽位编辑中改态。",
        )
    elif trough.status != Trough.STATUS_READY and reason:
        messages.info(request, f"{trough}：{reason}")


class BatchListView(LoginRequiredMixin, ListView):
    model = WitherBatch
    template_name = "batches/list.html"
    context_object_name = "batches"

    def get_queryset(self):
        return WitherBatch.objects.select_related("trough", "trough__garden").all()

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "batches/_table.html",
                {"batches": self.object_list},
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class BatchDetailView(LoginRequiredMixin, DetailView):
    model = WitherBatch
    template_name = "batches/detail.html"
    context_object_name = "batch"

    def get_queryset(self):
        return WitherBatch.objects.select_related("trough", "trough__garden")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        batch = self.object
        trough = batch.trough
        # 槽位改态读的就是这两个值；批次详情直接展示同一来源，杜绝分叉。
        trough_latest = trough.latest_batch()
        ctx["trough_latest"] = trough_latest
        ctx["trough_latest_moisture"] = trough.latest_actual_moisture()
        ctx["is_trough_latest"] = (
            trough_latest is not None and trough_latest.pk == batch.pk
        )
        ctx["ready_reason"] = trough.ready_block_reason()
        return ctx


class BatchCreateView(LoginRequiredMixin, CreateView):
    model = WitherBatch
    form_class = WitherBatchForm
    template_name = "batches/form.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        was_ready = form.cleaned_data["trough"].status == Trough.STATUS_READY
        trough_id = form.cleaned_data["trough"].pk
        response = super().form_valid(form)
        # 实测写完立刻影响所属槽的可下槽资格（模型层已兜底退回，这里出提示）。
        _announce_eligibility(self.request, trough_id, was_ready)
        messages.success(self.request, "萎凋批次已创建")
        return response


class BatchUpdateView(LoginRequiredMixin, UpdateView):
    model = WitherBatch
    form_class = WitherBatchForm
    template_name = "batches/form.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        # 批次可能被改挂到别的槽：保存前后的槽资格都要重算（模型层兜底）。
        old_trough = self.object.trough
        old_was_ready = old_trough.status == Trough.STATUS_READY
        new_trough = form.cleaned_data["trough"]
        new_was_ready = new_trough.status == Trough.STATUS_READY
        response = super().form_valid(form)
        new_trough_id = self.object.trough_id
        _announce_eligibility(self.request, new_trough_id, new_was_ready)
        if old_trough.pk != new_trough_id:
            _announce_eligibility(self.request, old_trough.pk, old_was_ready)
        messages.success(self.request, "萎凋批次已更新")
        return response


class BatchDeleteView(LoginRequiredMixin, DeleteView):
    model = WitherBatch
    template_name = "batches/confirm_delete.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        trough = self.object.trough
        was_ready = trough.status == Trough.STATUS_READY
        trough_id = trough.pk
        response = super().form_valid(form)
        messages.success(self.request, "萎凋批次已删除")
        # 删掉最新批次同样立刻影响资格（post_delete 信号兜底，这里出提示）。
        _announce_eligibility(self.request, trough_id, was_ready)
        return response
