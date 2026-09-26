from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_delete
from django.utils.translation import gettext_lazy as _

# 实测含水率（%）的合法开区间下界、闭区间上界。
# 表单校验与模型校验共用这两个常量与下面的校验器，避免双口径。
ACTUAL_MOISTURE_MIN_EXCLUSIVE = Decimal("0")
ACTUAL_MOISTURE_MAX = Decimal("100")

# 可下槽门槛：最新批次实测含水率不得高于该值。
READY_MOISTURE_MAX = Decimal("40")


def validate_actual_moisture(value):
    """实测含水率：允许为空（调用方须允许 null）；填写时必须 0 < 值 <= 100。

    同一份校验器同时挂在模型字段与 ModelForm 上（字段 validators 会被
    ModelForm 自动继承），表单提交与直接模型保存两条路径共用同一文案。
    """
    if value is None:
        return
    if value <= ACTUAL_MOISTURE_MIN_EXCLUSIVE or value > ACTUAL_MOISTURE_MAX:
        raise ValidationError(
            _("实测含水率填写时必须大于 0 且不超过 100。")
        )


class Garden(models.Model):
    name = models.CharField("茶园名称", max_length=120)
    altitudeBand = models.CharField("海拔带", max_length=60)
    notes = models.TextField("备注", blank=True, default="")

    class Meta:
        ordering = ["name"]
        verbose_name = "茶园"
        verbose_name_plural = "茶园"

    def __str__(self):
        return self.name


class TroughQuerySet(models.QuerySet):
    def ready(self):
        """当前状态已是「可下槽」的槽——首页计数与列表筛选的唯一口径。"""
        return self.filter(status=Trough.STATUS_READY)


class Trough(models.Model):
    STATUS_LOADING = "loading"
    STATUS_WITHERING = "withering"
    STATUS_READY = "ready"
    STATUS_CHOICES = [
        (STATUS_LOADING, "装叶中"),
        (STATUS_WITHERING, "萎凋中"),
        (STATUS_READY, "可下槽"),
    ]

    garden = models.ForeignKey(
        Garden,
        on_delete=models.CASCADE,
        related_name="troughs",
        verbose_name="茶园",
    )
    troughCode = models.CharField("槽位编号", max_length=40)
    cultivar = models.CharField("茶树品种", max_length=80)
    loadKg = models.DecimalField("装叶量(kg)", max_digits=10, decimal_places=2)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_LOADING,
    )

    objects = TroughQuerySet.as_manager()

    class Meta:
        ordering = ["garden__name", "troughCode"]
        verbose_name = "萎凋槽"
        verbose_name_plural = "萎凋槽"
        constraints = [
            models.UniqueConstraint(
                fields=["garden", "troughCode"],
                name="uniq_trough_code_per_garden",
            ),
        ]

    def __str__(self):
        return f"{self.garden.name}-{self.troughCode}"

    def latest_batch(self):
        """该槽最新一笔萎凋批次——改态判定与各详情页共用的唯一读取入口。"""
        return self.batches.all().order_by("-startedAt", "-id").first()

    def latest_actual_moisture(self):
        """最新批次的实测含水率；无批次或未填实测均返回 None。"""
        latest = self.latest_batch()
        return latest.actualMoisture if latest is not None else None

    def ready_block_reason(self):
        """当前不能改标「可下槽」的中文原因；满足资格时返回 None。

        空实测（或根本没有批次）不算可下槽；实测高于门槛同样拦截。
        """
        moisture = self.latest_actual_moisture()
        if moisture is None:
            return "无法设为可下槽：最新萎凋批次尚未填写实测含水率。"
        if moisture > READY_MOISTURE_MAX:
            return (
                f"无法设为可下槽：最新批次实测含水率 {moisture}% 高于 "
                f"{READY_MOISTURE_MAX}% 的门槛。"
            )
        return None

    def is_ready_eligible(self):
        return self.ready_block_reason() is None

    def enforce_ready_eligibility(self):
        """按同源数据立即兜底资格：已是「可下槽」却不再合格则退回「萎凋中」。

        批次保存/删除（含直接走模型的路径）后调用，保证「写完立刻影响
        可下槽资格」在表单与模型两条路径都成立。用 queryset.update 只改状态列，
        避免再次触发 full_clean 链路。
        """
        if self.status == self.STATUS_READY and self.ready_block_reason():
            Trough.objects.filter(pk=self.pk, status=self.STATUS_READY).update(
                status=self.STATUS_WITHERING
            )
            self.status = self.STATUS_WITHERING

    def clean(self):
        super().clean()
        if self.status != self.STATUS_READY:
            return
        reason = self.ready_block_reason()
        if reason:
            raise ValidationError({"status": reason})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class WitherBatch(models.Model):
    trough = models.ForeignKey(
        Trough,
        on_delete=models.CASCADE,
        related_name="batches",
        verbose_name="萎凋槽",
    )
    startedAt = models.DateTimeField("开始时间")
    targetMoisture = models.DecimalField(
        "目标含水率(%)", max_digits=5, decimal_places=2
    )
    actualMoisture = models.DecimalField(
        "实测含水率(%)",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[validate_actual_moisture],
    )
    rollGrade = models.CharField("揉捻等级", max_length=40)

    class Meta:
        ordering = ["-startedAt", "-id"]
        verbose_name = "萎凋批次"
        verbose_name_plural = "萎凋批次"

    def __str__(self):
        return f"{self.trough} @ {self.startedAt:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        # 直接模型保存也必须过表单同一套校验：非法实测中文拒绝，空实测放行。
        old_trough_id = None
        if self.pk:
            old_trough_id = (
                WitherBatch.objects.filter(pk=self.pk)
                .values_list("trough_id", flat=True)
                .first()
            )
        self.full_clean()
        super().save(*args, **kwargs)
        # 写完立刻影响所属槽资格（表单视图另会给出中文提示；这里是模型层兜底，
        # 直写模型、shell、admin 等路径同样生效）。
        self.trough.enforce_ready_eligibility()
        if old_trough_id and old_trough_id != self.trough_id:
            Trough.objects.get(pk=old_trough_id).enforce_ready_eligibility()


def _batch_deleted_refresh_trough(sender, instance, **kwargs):
    instance.trough.enforce_ready_eligibility()


post_delete.connect(_batch_deleted_refresh_trough, sender=WitherBatch)
