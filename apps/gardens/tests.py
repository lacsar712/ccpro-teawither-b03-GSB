from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.gardens.forms import WitherBatchForm
from apps.gardens.models import (
    ACTUAL_MOISTURE_MESSAGE,
    READY_REJECT_MESSAGE,
    Garden,
    Trough,
    WitherBatch,
)
from apps.gardens.seed import ensure_seed_data


class ActualMoistureValidationTests(TestCase):
    """实测含水率：表单校验与模型保存必须走同一套规则、同一句中文。"""

    def setUp(self):
        self.garden = Garden.objects.create(name="测试园", altitudeBand="600m")
        self.trough = Trough.objects.create(
            garden=self.garden,
            troughCode="T-1",
            cultivar="品种",
            loadKg=Decimal("100.00"),
        )
        self.started = timezone.now().strftime("%Y-%m-%dT%H:%M")

    def _model_save(self, value):
        batch = WitherBatch(
            trough=self.trough,
            startedAt=timezone.now(),
            targetMoisture=Decimal("40.00"),
            actualMoisture=None if value is None else Decimal(value),
            rollGrade="一级",
        )
        batch.save()
        return batch

    def test_model_rejects_out_of_range_with_chinese_message(self):
        for value in ["0", "-1", "100.01", "120"]:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as ctx:
                    self._model_save(value)
                self.assertIn(
                    ACTUAL_MOISTURE_MESSAGE, ctx.exception.messages
                )

    def test_form_rejects_out_of_range_with_same_chinese_message(self):
        for value in ["0", "-1", "100.01", "120"]:
            with self.subTest(value=value):
                form = WitherBatchForm(
                    data={
                        "trough": self.trough.pk,
                        "startedAt": self.started,
                        "targetMoisture": "40.00",
                        "actualMoisture": value,
                        "rollGrade": "一级",
                    }
                )
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors["actualMoisture"], [ACTUAL_MOISTURE_MESSAGE]
                )

    def test_model_and_form_reject_message_identical(self):
        # 模型直接保存的拒绝文案
        try:
            self._model_save("120")
        except ValidationError as exc:
            model_message = exc.message_dict["actualMoisture"][0]
        else:
            self.fail("模型保存 120 应被拒绝")

        form = WitherBatchForm(
            data={
                "trough": self.trough.pk,
                "startedAt": self.started,
                "targetMoisture": "40.00",
                "actualMoisture": "120",
                "rollGrade": "一级",
            }
        )
        form.is_valid()
        form_message = form.errors["actualMoisture"][0]
        self.assertEqual(model_message, form_message)

    def test_empty_actual_moisture_allowed_model_and_form(self):
        batch = self._model_save(None)
        self.assertIsNone(batch.actualMoisture)

        form = WitherBatchForm(
            data={
                "trough": self.trough.pk,
                "startedAt": self.started,
                "targetMoisture": "40.00",
                "actualMoisture": "",
                "rollGrade": "一级",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            WitherBatch.objects.filter(actualMoisture__isnull=True).count(), 2
        )

    def test_boundary_values_accepted(self):
        # 100.00 合法（不超过 100）；0.01 合法（大于 0）。
        self._model_save("100")
        self._model_save("0.01")


class ReadyQualificationTests(TestCase):
    def setUp(self):
        self.garden = Garden.objects.create(name="测试园", altitudeBand="600m")
        self.trough = Trough.objects.create(
            garden=self.garden,
            troughCode="T-1",
            cultivar="品种",
            loadKg=Decimal("100.00"),
        )
        now = timezone.now()
        self.batch = WitherBatch.objects.create(
            trough=self.trough,
            startedAt=now - timezone.timedelta(hours=2),
            targetMoisture=Decimal("38.00"),
            actualMoisture=Decimal("37.50"),
            rollGrade="一级",
        )

    def _set_ready(self):
        self.trough.status = Trough.STATUS_READY
        self.trough.save()

    def test_valid_moisture_allows_ready(self):
        self._set_ready()
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_READY)
        self.assertTrue(self.trough.ready_eligible())

    def test_empty_moisture_does_not_grant_ready(self):
        self.batch.actualMoisture = None
        self.batch.save()
        self.trough.refresh_from_db()
        self.assertFalse(self.trough.ready_eligible())
        with self.assertRaises(ValidationError) as ctx:
            self._set_ready()
        self.assertEqual(
            ctx.exception.message_dict["status"], [READY_REJECT_MESSAGE]
        )
        self.trough.refresh_from_db()
        self.assertNotEqual(self.trough.status, Trough.STATUS_READY)

    def test_over_limit_moisture_does_not_grant_ready(self):
        self.batch.actualMoisture = Decimal("42.00")
        self.batch.save()
        self.trough.refresh_from_db()
        self.assertFalse(self.trough.ready_eligible())
        with self.assertRaises(ValidationError):
            self._set_ready()
        self.trough.refresh_from_db()
        self.assertNotEqual(self.trough.status, Trough.STATUS_READY)

    def test_new_empty_batch_immediately_revokes_ready(self):
        """写完立刻影响资格：原可下槽槽，新批次实测为空，立即回退。"""
        self._set_ready()
        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now(),
            targetMoisture=Decimal("40.00"),
            actualMoisture=None,
            rollGrade="待评",
        )
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_WITHERING)

    def test_new_over_limit_batch_immediately_revokes_ready(self):
        self._set_ready()
        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now(),
            targetMoisture=Decimal("40.00"),
            actualMoisture=Decimal("55.00"),
            rollGrade="待评",
        )
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_WITHERING)

    def test_updating_latest_batch_to_empty_revokes_ready(self):
        # 可下槽槽已有合格最新批次（36%）；把该最新批次实测清空 -> 立即回退。
        self._set_ready()
        newer = WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now(),
            targetMoisture=Decimal("40.00"),
            actualMoisture=Decimal("36.00"),
            rollGrade="特级",
        )
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_READY)

        newer.actualMoisture = None
        newer.save()
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_WITHERING)

    def test_delete_only_valid_batch_revokes_ready(self):
        self._set_ready()
        self.batch.delete()
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_WITHERING)

    def test_newer_valid_batch_is_latest_and_keeps_ready(self):
        self._set_ready()
        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now(),
            targetMoisture=Decimal("40.00"),
            actualMoisture=Decimal("39.90"),
            rollGrade="特级",
        )
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_READY)
        self.assertEqual(
            self.trough.latest_actual_moisture(), Decimal("39.90")
        )


class SameSourceReadTests(TestCase):
    """槽改态入口读数与详情/列表展示必须同源（latest_batch）。"""

    def setUp(self):
        garden = Garden.objects.create(name="测试园", altitudeBand="600m")
        self.trough = Trough.objects.create(
            garden=garden,
            troughCode="T-9",
            cultivar="品种",
            loadKg=Decimal("80.00"),
        )
        now = timezone.now()
        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=now - timezone.timedelta(hours=10),
            targetMoisture=Decimal("40.00"),
            actualMoisture=Decimal("35.00"),
            rollGrade="旧",
        )
        self.latest = WitherBatch.objects.create(
            trough=self.trough,
            startedAt=now - timezone.timedelta(hours=1),
            targetMoisture=Decimal("40.00"),
            actualMoisture=Decimal("45.00"),
            rollGrade="新",
        )

    def test_latest_actual_moisture_matches_latest_batch(self):
        self.assertEqual(
            self.trough.latest_batch(), self.latest
        )
        self.assertEqual(
            self.trough.latest_actual_moisture(),
            self.trough.latest_batch().actualMoisture,
        )
        self.assertEqual(
            self.trough.latest_actual_moisture(), Decimal("45.00")
        )

    def test_ready_eligibility_uses_same_latest_reading(self):
        # 最新 45（失格）；旧的 35 合格也不得用于改态——读数同源。
        self.assertFalse(self.trough.ready_eligible())
        with self.assertRaises(ValidationError):
            self.trough.status = Trough.STATUS_READY
            self.trough.save()

    def test_list_page_shows_same_latest_reading(self):
        user = get_user_model().objects.create_user("u", password="x")
        self.client.force_login(user)
        resp = self.client.get(reverse("trough_list"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("45.00%", content)  # 最新批次实测
        self.assertNotIn("35.00%", content)  # 非最新批次不应作为槽读数


class HomeReadyCountReconciliationTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("u", password="x")
        self.client.force_login(user)
        garden = Garden.objects.create(name="测试园", altitudeBand="600m")
        now = timezone.now()

        # 两笔可下槽
        for code, moisture in [("R-1", "34.00"), ("R-2", "40.00")]:
            t = Trough.objects.create(
                garden=garden, troughCode=code, cultivar="v",
                loadKg=Decimal("50.00"),
            )
            WitherBatch.objects.create(
                trough=t, startedAt=now, targetMoisture=Decimal("40.00"),
                actualMoisture=Decimal(moisture), rollGrade="x",
            )
            t.status = Trough.STATUS_READY
            t.save()

        # 一笔空实测、一笔越界：均非可下槽
        t_missing = Trough.objects.create(
            garden=garden, troughCode="N-1", cultivar="v",
            loadKg=Decimal("50.00"),
        )
        WitherBatch.objects.create(
            trough=t_missing, startedAt=now, targetMoisture=Decimal("40.00"),
            actualMoisture=None, rollGrade="x",
        )
        t_over = Trough.objects.create(
            garden=garden, troughCode="N-2", cultivar="v",
            loadKg=Decimal("50.00"),
        )
        WitherBatch.objects.create(
            trough=t_over, startedAt=now, targetMoisture=Decimal("40.00"),
            actualMoisture=Decimal("42.00"), rollGrade="x",
        )

    def test_home_count_equals_ready_filter_rows(self):
        home = self.client.get(reverse("home"))
        ready_db = Trough.objects.filter(status=Trough.STATUS_READY).count()
        self.assertEqual(home.context["ready_count"], ready_db)
        self.assertEqual(ready_db, 2)

        listing = self.client.get(
            reverse("trough_list"), {"status": Trough.STATUS_READY}
        )
        rows = list(listing.context["object_list"])
        self.assertEqual(len(rows), ready_db)
        self.assertTrue(all(t.status == Trough.STATUS_READY for t in rows))

        # 首页卡片链接到可下槽筛选
        self.assertContains(
            home, reverse("trough_list") + "?status=ready"
        )

    def test_other_status_filters_exclude_ready(self):
        listing = self.client.get(
            reverse("trough_list"), {"status": Trough.STATUS_WITHERING}
        )
        self.assertEqual(
            listing.context["object_list"].count(),
            Trough.objects.filter(
                status=Trough.STATUS_WITHERING
            ).count(),
        )


class ViewPostValidationTests(TestCase):
    """表单提交（HTTP POST）路径同样以中文拒绝，且不入库。"""

    def setUp(self):
        user = get_user_model().objects.create_user("u", password="x")
        self.client.force_login(user)
        garden = Garden.objects.create(name="测试园", altitudeBand="600m")
        self.trough = Trough.objects.create(
            garden=garden, troughCode="T-1", cultivar="v",
            loadKg=Decimal("100.00"),
        )

    def test_batch_create_post_rejects_over_limit_value(self):
        before = WitherBatch.objects.count()
        resp = self.client.post(
            reverse("batch_create"),
            {
                "trough": self.trough.pk,
                "startedAt": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                "targetMoisture": "40.00",
                "actualMoisture": "120",
                "rollGrade": "一级",
            },
        )
        self.assertEqual(resp.status_code, 200)  # 表单重新渲染而非跳转
        self.assertContains(resp, ACTUAL_MOISTURE_MESSAGE)
        self.assertEqual(WitherBatch.objects.count(), before)

    def test_trough_edit_post_rejects_ready_when_missing_moisture(self):
        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now(),
            targetMoisture=Decimal("40.00"),
            actualMoisture=None,
            rollGrade="待评",
        )
        resp = self.client.post(
            reverse("trough_edit", args=[self.trough.pk]),
            {
                "garden": self.trough.garden_id,
                "troughCode": self.trough.troughCode,
                "cultivar": self.trough.cultivar,
                "loadKg": "100.00",
                "status": Trough.STATUS_READY,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, READY_REJECT_MESSAGE)
        self.trough.refresh_from_db()
        self.assertNotEqual(self.trough.status, Trough.STATUS_READY)


class SeedDataTests(TestCase):
    def test_seed_includes_missing_and_over_limit_negative_examples(self):
        ensure_seed_data()

        t_missing = Trough.objects.get(
            garden__name="云雾岭一号园", troughCode="A-02"
        )
        t_over = Trough.objects.get(
            garden__name="竹影台二号园", troughCode="B-01"
        )
        t_ready = Trough.objects.get(
            garden__name="竹影台二号园", troughCode="B-02"
        )

        # 缺实测槽：批次可存，但不可下槽
        self.assertIsNone(t_missing.latest_actual_moisture())
        self.assertFalse(t_missing.ready_eligible())
        self.assertNotEqual(t_missing.status, Trough.STATUS_READY)

        # 越界实测槽：42% 在字段 0-100 内可存，但越过 40 门槛不可下槽
        self.assertEqual(t_over.latest_actual_moisture(), Decimal("42.00"))
        self.assertFalse(t_over.ready_eligible())
        self.assertNotEqual(t_over.status, Trough.STATUS_READY)

        # 正向对照：34.8% 可下槽
        self.assertEqual(t_ready.status, Trough.STATUS_READY)
        self.assertTrue(t_ready.ready_eligible())

        # 两笔反例直接模型改态都被中文拒绝
        for trough in (t_missing, t_over):
            with self.subTest(trough=trough.troughCode):
                trough.status = Trough.STATUS_READY
                with self.assertRaises(ValidationError) as ctx:
                    trough.save()
                self.assertEqual(
                    ctx.exception.message_dict["status"],
                    [READY_REJECT_MESSAGE],
                )
