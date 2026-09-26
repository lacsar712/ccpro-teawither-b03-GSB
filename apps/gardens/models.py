from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

# 实测含水率：可空；一旦填写必须大于 0 且不超过 100。
ACTUAL_MOISTURE_MIN = Decimal("0")
ACTUAL_MOISTURE_MAX = Decimal("100")
ACTUAL_MOISTURE_MESSAGE = "实测含水率若填写须大于 0 且不超过 100。"

# 可下槽门槛：最新批次实测含水率须已填写且不超过该值。
READY_MOISTURE_LIMIT = Decimal("40")
READY_REJECT_MESSAGE = (
    "无法设为可下槽：最新萎凋批次的实测含水率为空或高于 40%。"
)


def validate_actual_moisture(value):
    """实测含水率唯一规则源：空值放行（可空字段），非空须 0 < 值 <= 100。

    表单字段经模型字段自动挂上同一 validator，直接调用模型保存时由
    WitherBatch.save -> full_clean 走同一函数，两条路径文案口径一致。
    """
    if value is None:
        return
    if value <= ACTUAL_MOISTURE_MIN or value > ACTUAL_MOISTURE_MAX:
        raise ValidationError(ACTUAL_MOISTURE_MESSAGE)


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
        """最新批次的唯一取数入口：改态校验与详情/列表读数都经此方法。"""
        return self.batches.order_by("-startedAt", "-id").first()

    def latest_actual_moisture(self):
        """最新批次的实测含水率（可能为空）。槽改态入口与页面读数同源。"""
        latest = self.latest_batch()
        return latest.actualMoisture if latest else None

    def ready_eligible(self):
        """是否满足可下槽资格：最新批次实测已填且不超过门槛。空实测不放行。"""
        moisture = self.latest_actual_moisture()
        return moisture is not None and moisture <= READY_MOISTURE_LIMIT

    def sync_ready_status(self):
        """按最新批次实测重算资格：失格的可下槽槽立即回退为萎凋中。

        在批次写入/删除后调用，确保实测一保存就影响可下槽资格。
        仅在失格时回退，资格满足不自动晋升（改态仍走人工入口与 clean 校验）。
        """
        if self.status == self.STATUS_READY and not self.ready_eligible():
            self.status = self.STATUS_WITHERING
            super().save(update_fields=["status"])

    def clean(self):
        super().clean()
        if self.status != self.STATUS_READY:
            return
        if not self.ready_eligible():
            raise ValidationError({"status": READY_REJECT_MESSAGE})

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
        # 直接模型保存路径：与 ModelForm 提交走同一条 full_clean + 字段 validator。
        self.full_clean()
        super().save(*args, **kwargs)
        # 实测一落库立即重算所属槽资格（失格则可下槽槽回退）。
        self.trough.sync_ready_status()

    def delete(self, *args, **kwargs):
        trough = self.trough
        super().delete(*args, **kwargs)
        # 最新批次被删后，所属槽可能因失去合格实测而失格。
        trough.sync_ready_status()
