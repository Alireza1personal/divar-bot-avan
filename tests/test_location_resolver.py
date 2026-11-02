# -*- coding: utf-8 -*-
import os, sys, types, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for k, v in {"TELEGRAM_BOT_TOKEN": "x", "CHAT_ID": "1", "TOPIC_SELL": "1", "TOPIC_RENT": "2"}.items():
    os.environ.setdefault(k, v)
try:
    import telegram, dotenv  # noqa
except ImportError:
    t = types.ModuleType("telegram"); t.Bot = object; sys.modules["telegram"] = t
    d = types.ModuleType("dotenv"); d.load_dotenv = lambda *a, **k: None; sys.modules["dotenv"] = d
from location_resolver import LocationResolver, fold_key
import main

def nb(i, n): return {"id": str(i), "name": n}
TREE = [{"id": "3", "name": "مشهد", "regions": [
    {"id": "9", "name": "۹", "neighborhoods": [nb(1, "هفت تیر(آب و برق)"), nb(2, "هفت تیر(حافظ)"), nb(3, "هفت تیر(نه دره)"), nb(4, "زیباشهر")]},
    {"id": "11", "name": "۱۱", "neighborhoods": [nb(5, "وکیل آباد"), nb(6, "زیباشهر")]},   # same name under 2 regions
]}]
def M(raw, pid, city="مشهد", region="", t="neighborhood"):
    return {"rawKey": fold_key(raw), "rawText": raw, "cityKey": fold_key(city), "regionKey": fold_key(region), "placeType": t, "placeId": str(pid), "neighborhoodId": str(pid)}

def R(mappings=()):
    r = LocationResolver(); r.load(list(mappings), TREE); return r

class T(unittest.TestCase):
    def test_mapping_city_raw(self):
        r = R([M("شهرک غرب", 5)]).resolve("شهرک غرب", "")
        self.assertFalse(r.needs_review); self.assertEqual((r.region, r.neighborhood, r.source), ("۱۱", "وکیل آباد", "mapping"))
    def test_mapping_full_context_beats_city_only(self):
        r = R([M("جاده قدیم", 5), M("جاده قدیم", 1, region="۹")]).resolve("جاده قدیم", "واقع در منطقه ۹ نزدیک آب و برق")
        self.assertEqual(r.neighborhood, "هفت تیر(آب و برق)")
    def test_mapping_raw_alone(self):
        r = R([M("خیابان x", 5, city="")]).resolve("خیابان X", "")
        self.assertEqual(r.neighborhood, "وکیل آباد")
    def test_folding(self):
        r = R([M("وكيل آباد‌ نو", 5)]).resolve("وکیل‌آباد نو", "")  # ك/ي, ZWNJ
        self.assertEqual(r.source, "mapping")
    def test_duplicate_generic_without_evidence_needs_review(self):
        r = R().resolve("هفت تیر", "آپارتمان تمیز")
        self.assertTrue(r.needs_review); self.assertEqual((r.city, r.region, r.neighborhood), ("مشهد", "", "هفت تیر"))
    def test_duplicate_resolved_by_description(self):
        r = R().resolve("هفت تیر", "نزدیک نه دره")
        self.assertEqual((r.neighborhood, r.source), ("هفت تیر(نه دره)", "tree"))
    def test_duplicate_two_qualifiers_ambiguous(self):
        self.assertTrue(R().resolve("هفت تیر", "بین حافظ و آب و برق").needs_review)
    def test_same_name_two_regions_uses_region_text(self):
        r = R().resolve("زیباشهر", "منطقه ۱۱")
        self.assertEqual((r.region, r.neighborhood), ("۱۱", "زیباشهر"))
        self.assertTrue(R().resolve("زیباشهر", "").needs_review)
    def test_learned_generic_mapping_cannot_guess(self):
        # admin once linked bare «هفت تیر» → حافظ ; without evidence in this ad it must NOT be reused
        self.assertTrue(R([M("هفت تیر", 2)]).resolve("هفت تیر", "").needs_review)
        self.assertEqual(R([M("هفت تیر", 2)]).resolve("هفت تیر", "حافظ").neighborhood, "هفت تیر(حافظ)")
    def test_unknown_needs_review_keeps_raw(self):
        r = R().resolve("محله ناشناخته", "")
        self.assertEqual((r.needs_review, r.region, r.neighborhood), (True, "", "محله ناشناخته"))
    def test_static_fallback_when_tree_empty(self):
        r = LocationResolver(); r.load([], [])
        a = r.resolve("وکیل آباد", ""); self.assertEqual((a.region, a.neighborhood, a.source), ("۱۱", "وکیل آباد", "static"))
        self.assertTrue(r.resolve("هفت تیر", "").needs_review)             # old code defaulted to حافظ
        self.assertEqual(r.resolve("هفت تیر", "آب و برق").neighborhood, "هفت تیر(آب و برق)")
    def test_dormant_mapping_ignored(self):
        self.assertTrue(R([M("جای حذف شده", 999)]).resolve("جای حذف شده", "").needs_review)
    def test_payload_end_to_end(self):
        main.MAPPER.load([], TREE)
        def post(district, desc):
            details = {"webengage": {"business_type": "personal"}, "sections": [{"widgets": [
                {"data": {"@type": "x.DescriptionRowData", "text": desc}},
                {"data": {"@type": "x.UnexpandableRowData", "title": "قیمت کل", "value": "۲٬۰۰۰٬۰۰۰٬۰۰۰"}}]}]}
            cand = {"token": "tok", "district": district, "title": "فروش", "category": "apartment-sell", "catLabel": "فروش"}
            return main.is_owner_and_format(details, cand)["panel_payload"]
        ok = post("وکیل آباد", "خوب"); self.assertEqual((ok["region"], ok["neighborhood"]), ("۱۱", "وکیل آباد")); self.assertNotIn("needs_review", ok)
        bad = post("هفت تیر", "خوب"); self.assertEqual((bad["city"], bad["region"], bad["neighborhood"], bad["needs_review"]), ("مشهد", "", "هفت تیر", True))
        self.assertEqual(post("هفت تیر", "کنار حافظ")["neighborhood"], "هفت تیر(حافظ)")

if __name__ == "__main__":
    unittest.main(verbosity=2)
