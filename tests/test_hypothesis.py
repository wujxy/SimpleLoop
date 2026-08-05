from __future__ import annotations

from simpleloop.roles.hypothesis import (
    HypothesisCard,
    dedup_by_signature,
    distinct_niches,
)


def _card(op="G6", region="OMILRECV2/src/Rec/EVLikelihood.cc", mech="repeated-lookup",
          interv="cache", slot="guided"):
    return HypothesisCard(
        generative_op=op, region=region, mechanism=mech,
        intervention_family=interv, why_plausible="x", critical_unknown="y",
        slot=slot,
    )


class TestSignature:
    def test_path_region_is_file_bucketed(self):
        c = _card(region="OMILRECV2/src/Rec/EVLikelihood.cc:Calc")
        # _bucket_path drops :FunctionName
        assert c.signature()[0] == "OMILRECV2/src/Rec/EVLikelihood.cc"

    def test_tag_region_is_normalized(self):
        c = _card(region="geometry")
        assert c.signature()[0] == "geometry"

    def test_mechanism_canonicalized_lowercase_whitespace(self):
        a = _card(mech="Repeated Lookup")
        b = _card(mech="repeated   lookup")
        assert a.signature()[1] == b.signature()[1] == "repeated lookup"

    def test_intervention_canonicalized(self):
        a = _card(interv="Cache Precompute")
        b = _card(interv="cache  precompute")
        assert a.signature()[2] == b.signature()[2]

    def test_empty_fields_become_unknown(self):
        c = _card(region="", mech="", interv="")
        sig = c.signature()
        assert sig == ("unknown-region", "unknown", "unknown")


class TestDedup:
    def test_same_signature_collapses_to_one(self):
        cards = [_card(mech="cache"), _card(mech="cache"), _card(mech="cache")]
        out = dedup_by_signature(cards, per_bin=1)
        assert len(out) == 1

    def test_different_signatures_all_kept(self):
        cards = [
            _card(mech="cache", interv="hoist"),
            _card(mech="search", interv="algorithm-replace"),
            _card(mech="alloc", interv="reuse"),
        ]
        out = dedup_by_signature(cards, per_bin=1)
        assert len(out) == 3

    def test_per_bin_two_keeps_two(self):
        cards = [_card(mech="cache"), _card(mech="cache"),
                 _card(mech="search"), _card(mech="search")]
        out = dedup_by_signature(cards, per_bin=2)
        assert len(out) == 4

    def test_rotation_not_always_first(self):
        """When two cards share a sig, the kept one should not always be
        index [0] — deterministic rotation by output position."""
        cards = [
            _card(mech="cache", interv="v1"),
            _card(mech="cache", interv="v2"),
        ]
        # v1 and v2 share mech="cache" but differ in interv → different sigs.
        # To test rotation within one bin, use identical sigs:
        cards_same = [
            _card(mech="cache", interv="cache", slot="guided"),
            _card(mech="cache", interv="cache", slot="free"),
        ]
        preceding = [_card(mech="search", interv="x")]
        out = dedup_by_signature(preceding + cards_same, per_bin=1)
        # First niche (search) kept. Second bin (cache): offset = 1 % 2 = 1
        # → keeps the second card (slot="free"), not the first.
        assert out[0].intervention_family == "x"
        assert out[1].slot == "free"

    def test_preserves_cross_niche_order(self):
        cards = [
            _card(mech="cache", interv="a"),
            _card(mech="search", interv="b"),
            _card(mech="alloc", interv="c"),
        ]
        out = dedup_by_signature(cards, per_bin=1)
        assert [c.mechanism for c in out] == ["cache", "search", "alloc"]


class TestDistinctNiches:
    def test_counts_unique_signatures(self):
        cards = [
            _card(mech="cache", interv="a"),
            _card(mech="cache", interv="a"),  # dup
            _card(mech="search", interv="b"),
        ]
        assert distinct_niches(cards) == 2

    def test_empty(self):
        assert distinct_niches([]) == 0
