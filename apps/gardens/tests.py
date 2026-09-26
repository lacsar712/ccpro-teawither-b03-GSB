from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import WitherBatchForm
from .models import Garden, Trough, WitherBatch

MSG = "实测含水率填写时必须大于 0 且不超过 100。"


def local_form_time(dt=None):
    """datetime-local 控件提交的是本地时间（TIME_ZONE=Asia/Shanghai）。"""
    value = timezone.localtime(dt or timezone.now())
    return value.strftime("%Y-%m-%dT%H:%M")


def make_garden(name="测试园"):
    return Garden.objects.create(name=name, altitudeBand="600-800m")


def make_trough(garden=None, code="T-1", status=Trough.STATUS_WITHERING):
    return Trough.objects.create(
        garden=garden or make_garden(),
        troughCode=code,
        cultivar="福鼎大白",
        loadKg=Decimal("100.00"),
        status=status,
    )


def make_batch(trough, actual=Decimal("38.00"), hours_ago=1):
    return WitherBatch.objects.create(
        trough=trough,
        startedAt=timezone.now() - timezone.timedelta(hours=hours_ago),
        targetMoisture=Decimal("40.00"),
        actualMoisture=actual,
        rollGrade="一级",
    )


class ActualMoistureModelTests(TestCase):
    def test_none_allowed_direct_save(self):
        t = make_trough()
        b = make_batch(t, actual=None)
        b.refresh_from_db()
        self.assertIsNone(b.actualMoisture)

    def test_boundaries_allowed(self):
        t = make_trough()
        self.assertEqual(make_batch(t, actual=Decimal("0.01")).actualMoisture, Decimal("0.01"))
        self.assertEqual(make_batch(t, actual=Decimal("100.00")).actualMoisture, Decimal("100.00"))

    def test_invalid_direct_save_rejected_chinese(self):
        t = make_trough()
        for bad in ("0", "-1", "100.01", "120"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError) as ctx:
                    make_batch(t, actual=Decimal(bad))
                self.assertIn(MSG, ctx.exception.messages)

    def test_invalid_not_persisted(self):
        t = make_trough()
        with self.assertRaises(ValidationError):
            make_batch(t, actual=Decimal("120"))
        self.assertEqual(WitherBatch.objects.count(), 0)


class ActualMoistureFormTests(TestCase):
    def _post_data(self, trough, actual):
        return {
            "trough": str(trough.pk),
            "startedAt": local_form_time(),
            "targetMoisture": "40.00",
            "actualMoisture": actual,
            "rollGrade": "一级",
        }

    def test_form_rejects_same_chinese_wording_as_model(self):
        t = make_trough()
        for bad in ("0", "-5", "100.01", "120"):
            with self.subTest(bad=bad):
                form = WitherBatchForm(data=self._post_data(t, bad))
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["actualMoisture"], [MSG])

    def test_form_blank_saved_as_none(self):
        t = make_trough()
        form = WitherBatchForm(data=self._post_data(t, ""))
        self.assertTrue(form.errors.as_text() == "", form.errors.as_text())
        self.assertTrue(form.is_valid())
        b = form.save()
        self.assertIsNone(b.actualMoisture)

    def test_form_and_model_messages_identical(self):
        t = make_trough()
        form = WitherBatchForm(data=self._post_data(t, "120"))
        form.is_valid()
        form_msg = form.errors["actualMoisture"][0]
        try:
            WitherBatch(
                trough=t,
                startedAt=timezone.now(),
                targetMoisture=Decimal("40"),
                actualMoisture=Decimal("120"),
                rollGrade="x",
            ).full_clean()
            self.fail("模型 full_clean 应拒绝")
        except ValidationError as exc:
            model_msg = exc.message_dict["actualMoisture"][0]
        self.assertEqual(form_msg, model_msg)


class ReadyEligibilityModelTests(TestCase):
    def test_no_batch_blocks_ready(self):
        t = make_trough()
        self.assertIsNotNone(t.ready_block_reason())
        t.status = Trough.STATUS_READY
        with self.assertRaises(ValidationError):
            t.save()

    def test_empty_moisture_blocks_ready(self):
        t = make_trough()
        make_batch(t, actual=None)
        self.assertIsNotNone(t.ready_block_reason())
        t.status = Trough.STATUS_READY
        with self.assertRaises(ValidationError):
            t.save()

    def test_over_threshold_blocks_ready(self):
        t = make_trough()
        make_batch(t, actual=Decimal("42.00"))
        t.status = Trough.STATUS_READY
        with self.assertRaises(ValidationError):
            t.save()

    def test_threshold_boundary_and_below_allow_ready(self):
        for value in ("40.00", "34.80"):
            t = make_trough(code=f"T-{value}")
            make_batch(t, actual=Decimal(value))
            self.assertIsNone(t.ready_block_reason())
            t.status = Trough.STATUS_READY
            t.save()
            self.assertEqual(Trough.objects.get(pk=t.pk).status, Trough.STATUS_READY)

    def test_latest_batch_wins_by_started_at(self):
        t = make_trough()
        make_batch(t, actual=Decimal("35.00"), hours_ago=5)
        make_batch(t, actual=None, hours_ago=1)
        # 最新一笔缺实测 → 槽不可下槽，尽管更早一笔合格。
        self.assertIsNone(t.latest_actual_moisture())
        self.assertIsNotNone(t.ready_block_reason())

    def test_model_path_clearing_moisture_demotes_ready_immediately(self):
        # 不经视图：直接模型保存也要「写完立刻影响可下槽资格」。
        t = make_trough()
        b = make_batch(t, actual=Decimal("34.80"))
        t.status = Trough.STATUS_READY
        t.save()

        b.actualMoisture = None
        b.save()
        self.assertEqual(Trough.objects.get(pk=t.pk).status, Trough.STATUS_WITHERING)

        b.actualMoisture = Decimal("50")
        b.save()
        self.assertEqual(Trough.objects.get(pk=t.pk).status, Trough.STATUS_WITHERING)

    def test_model_path_deleting_latest_batch_demotes_ready(self):
        t = make_trough()
        b = make_batch(t, actual=Decimal("34.80"))
        t.status = Trough.STATUS_READY
        t.save()
        b.delete()
        self.assertEqual(Trough.objects.get(pk=t.pk).status, Trough.STATUS_WITHERING)


class EligibilityViewFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("u", password="x")
        self.client.force_login(self.user)
        self.trough = make_trough(status=Trough.STATUS_WITHERING)
        self.batch = make_batch(self.trough, actual=Decimal("34.80"))
        self.trough.status = Trough.STATUS_READY
        self.trough.save()

    def test_clearing_moisture_demotes_ready_trough_immediately(self):
        resp = self.client.post(
            reverse("batch_edit", args=[self.batch.pk]),
            {
                "trough": str(self.trough.pk),
                "startedAt": local_form_time(),
                "targetMoisture": "40.00",
                "actualMoisture": "",
                "rollGrade": "一级",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            Trough.objects.get(pk=self.trough.pk).status,
            Trough.STATUS_WITHERING,
        )

    def test_new_over_threshold_batch_demotes_ready_trough(self):
        resp = self.client.post(
            reverse("batch_create"),
            {
                "trough": str(self.trough.pk),
                "startedAt": local_form_time(),
                "targetMoisture": "40.00",
                "actualMoisture": "50",
                "rollGrade": "一级",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            Trough.objects.get(pk=self.trough.pk).status,
            Trough.STATUS_WITHERING,
        )

    def test_good_new_batch_keeps_status_but_announces_eligibility(self):
        trough = make_trough(code="T-2", status=Trough.STATUS_WITHERING)
        resp = self.client.post(
            reverse("batch_create"),
            {
                "trough": str(trough.pk),
                "startedAt": local_form_time(),
                "targetMoisture": "40.00",
                "actualMoisture": "38",
                "rollGrade": "一级",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            Trough.objects.get(pk=trough.pk).status, Trough.STATUS_WITHERING
        )
        self.assertTrue(trough.is_ready_eligible())

    def test_form_path_blocks_mark_ready_when_moisture_empty(self):
        trough = make_trough(code="T-3", status=Trough.STATUS_WITHERING)
        make_batch(trough, actual=None)
        resp = self.client.post(
            reverse("trough_edit", args=[trough.pk]),
            {
                "garden": str(trough.garden_id),
                "troughCode": trough.troughCode,
                "cultivar": trough.cultivar,
                "loadKg": "100.00",
                "status": Trough.STATUS_READY,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "无法设为可下槽")
        self.assertEqual(
            Trough.objects.get(pk=trough.pk).status, Trough.STATUS_WITHERING
        )

    def test_form_path_invalid_moisture_rejected_chinese(self):
        trough = make_trough(code="T-4", status=Trough.STATUS_WITHERING)
        resp = self.client.post(
            reverse("batch_create"),
            {
                "trough": str(trough.pk),
                "startedAt": local_form_time(),
                "targetMoisture": "40.00",
                "actualMoisture": "120",
                "rollGrade": "一级",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, MSG)
        # 非法批次不得落库：setUp 已有的 1 笔之外没有新增。
        self.assertEqual(WitherBatch.objects.count(), 1)


class SameSourceReadTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("u", password="x")
        self.client.force_login(self.user)
        self.trough = make_trough(status=Trough.STATUS_WITHERING)
        self.batch = make_batch(self.trough, actual=Decimal("37.50"))

    def test_edit_detail_and_batch_detail_show_same_value(self):
        edit = self.client.get(reverse("trough_edit", args=[self.trough.pk]))
        detail = self.client.get(reverse("trough_detail", args=[self.trough.pk]))
        bdetail = self.client.get(reverse("batch_detail", args=[self.batch.pk]))
        for resp in (edit, detail, bdetail):
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "37.50%")

    def test_empty_moisture_shown_consistently(self):
        b2 = make_batch(self.trough, actual=None, hours_ago=0)
        detail = self.client.get(reverse("trough_detail", args=[self.trough.pk]))
        bdetail = self.client.get(reverse("batch_detail", args=[b2.pk]))
        self.assertContains(detail, "未填写")
        self.assertContains(bdetail, "未填写")
        self.assertIsNone(self.trough.latest_actual_moisture())


class HomeCountReconcileTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("u", password="x")
        self.client.force_login(self.user)

    def test_home_count_matches_ready_filter_rows(self):
        t_ok = make_trough(code="OK-1")
        make_batch(t_ok, Decimal("34.80"))
        t_ok.status = Trough.STATUS_READY
        t_ok.save()

        t_empty = make_trough(code="EMPTY", status=Trough.STATUS_WITHERING)
        make_batch(t_empty, actual=None)

        t_high = make_trough(code="HIGH", status=Trough.STATUS_WITHERING)
        make_batch(t_high, Decimal("42"))

        home = self.client.get(reverse("home"))
        ready_qs = Trough.objects.ready()
        self.assertEqual(home.context["ready_count"], ready_qs.count())
        self.assertEqual(ready_qs.count(), 1)

        listing = self.client.get(reverse("trough_list"), {"status": "ready"})
        self.assertEqual(len(listing.context["object_list"]), ready_qs.count())
        self.assertEqual(listing.context["object_list"][0].pk, t_ok.pk)

        full = self.client.get(reverse("trough_list"))
        self.assertEqual(len(full.context["object_list"]), 3)
