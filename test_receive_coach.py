"""Basic test suite for receive_coach.py. Run with: python test_receive_coach.py"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

# Import under test
import receive_coach as rc


class TestSanitizeUserId(unittest.TestCase):
    def test_alphanumeric_preserved(self):
        self.assertEqual(rc.sanitize_user_id("justin123"), "justin123")

    def test_special_chars_replaced(self):
        self.assertEqual(rc.sanitize_user_id("justin@example.com"), "justin_example_com")

    def test_path_traversal_blocked(self):
        self.assertNotIn("/", rc.sanitize_user_id("../../../etc/passwd"))
        self.assertNotIn("\\", rc.sanitize_user_id("..\\windows\\system32"))

    def test_empty_falls_back_to_default(self):
        self.assertEqual(rc.sanitize_user_id(""), "default_user")
        self.assertEqual(rc.sanitize_user_id("   "), "default_user")

    def test_underscores_and_hyphens_preserved(self):
        self.assertEqual(rc.sanitize_user_id("test-user_1"), "test-user_1")


class TestExtractActionStep(unittest.TestCase):
    def test_matches_anchored_label(self):
        response = "Let's think about it.\nNext step: call your accountant tomorrow"
        self.assertEqual(
            rc.extract_action_step("", response),
            "call your accountant tomorrow",
        )

    def test_ignores_mid_sentence_occurrence(self):
        # The old over-greedy regex would match "step" in "step-mom"
        user_text = "My next step-mom drove me crazy"
        response = "Understood."
        self.assertEqual(rc.extract_action_step(user_text, response), "")

    def test_for_today_pattern(self):
        response = "For today: walk for 10 minutes before the meeting"
        self.assertIn("walk for 10 minutes", rc.extract_action_step("", response))

    def test_truncates_long_step(self):
        response = "Next step: " + "x" * 500
        self.assertLessEqual(len(rc.extract_action_step("", response)), 180)

    def test_empty_on_no_match(self):
        self.assertEqual(rc.extract_action_step("hi", "hi back"), "")

    def test_rejects_punctuation_only(self):
        response = "Next step: ..."
        self.assertEqual(rc.extract_action_step("", response), "")


class TestChunkText(unittest.TestCase):
    def test_splits_on_paragraphs(self):
        text = "First paragraph content here, about thirty chars.\n\nSecond paragraph, also long enough."
        chunks = rc.chunk_text(text, target=50)
        self.assertGreaterEqual(len(chunks), 2)

    def test_short_text_one_chunk(self):
        text = "Just one short paragraph with enough content to exceed the minimum length threshold."
        chunks = rc.chunk_text(text)
        self.assertEqual(len(chunks), 1)

    def test_drops_tiny_chunks(self):
        # Content below 30 chars should be dropped
        chunks = rc.chunk_text("tiny")
        self.assertEqual(len(chunks), 0)


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors_return_one(self):
        v = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(rc.cosine(v, v), 1.0, places=6)

    def test_orthogonal_vectors_return_zero(self):
        self.assertAlmostEqual(rc.cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_empty_vectors_return_zero(self):
        self.assertEqual(rc.cosine([], []), 0.0)
        self.assertEqual(rc.cosine([1.0], []), 0.0)

    def test_mismatched_length_returns_zero(self):
        self.assertEqual(rc.cosine([1.0, 2.0], [1.0, 2.0, 3.0]), 0.0)

    def test_zero_vector_returns_zero(self):
        self.assertEqual(rc.cosine([0.0, 0.0], [1.0, 1.0]), 0.0)


class TestDetectEmotionalState(unittest.TestCase):
    def test_overwhelmed(self):
        self.assertEqual(rc.detect_emotional_state("I feel so overwhelmed lately"), "overwhelmed")

    def test_stuck(self):
        self.assertEqual(rc.detect_emotional_state("I'm stuck in the same thing"), "stuck")

    def test_hopeful(self):
        self.assertEqual(rc.detect_emotional_state("Things are getting better"), "hopeful")

    def test_unclear_default(self):
        self.assertEqual(rc.detect_emotional_state("The weather is nice today"), "unclear")


class TestLoadCoaches(unittest.TestCase):
    def test_loads_at_least_one_coach(self):
        coaches = rc.load_coaches()
        self.assertGreater(len(coaches), 0)

    def test_general_coach_exists(self):
        coaches = rc.load_coaches()
        self.assertIn("general", coaches)
        self.assertTrue(coaches["general"].system_prompt)
        self.assertTrue(coaches["general"].model)

    def test_all_coaches_have_required_fields(self):
        coaches = rc.load_coaches()
        for name, coach in coaches.items():
            self.assertEqual(coach.name, name)
            self.assertTrue(coach.display_name, f"{name} missing display_name")
            self.assertTrue(coach.system_prompt, f"{name} missing system_prompt")
            self.assertTrue(coach.model, f"{name} missing model")


class TestMemoryManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_data_dir = rc.DATA_DIR
        rc.DATA_DIR = Path(self.tmp.name)

    def tearDown(self):
        rc.DATA_DIR = self._orig_data_dir
        self.tmp.cleanup()

    def test_new_user_creates_default_memory(self):
        mm = rc.MemoryManager("test_new")
        self.assertEqual(mm.data["user_id"], "test_new")
        self.assertEqual(mm.data["sessions"], [])
        self.assertIn("last_coach", mm.data)

    def test_add_session_updates_last_coach(self):
        mm = rc.MemoryManager("test_session")
        mm.add_session(
            coach="business",
            summary="test",
            main_issue="test",
            action_step="do thing",
            emotional_state="unclear",
            coach_notes="none",
        )
        self.assertEqual(mm.data["last_coach"], "business")
        self.assertEqual(len(mm.data["sessions"]), 1)

    def test_add_session_tags_coach(self):
        mm = rc.MemoryManager("test_tag")
        mm.add_session("business", "s", "i", "", "u", "n")
        self.assertEqual(mm.data["sessions"][0]["coach"], "business")

    def test_persistence_round_trip(self):
        mm1 = rc.MemoryManager("test_persist")
        mm1.add_session("general", "s", "i", "do it", "hopeful", "n")
        mm2 = rc.MemoryManager("test_persist")
        self.assertEqual(len(mm2.data["sessions"]), 1)
        self.assertEqual(mm2.data["last_coach"], "general")


class TestBuildMemorySummary(unittest.TestCase):
    def test_empty_memory(self):
        mem = rc.default_memory("x")
        self.assertEqual(rc.build_memory_summary(mem), "No prior memory.")

    def test_coach_filter(self):
        mem = rc.default_memory("x")
        mem["sessions"] = [
            {"session_id": "s1", "date": "2026-01-01 10:00:00", "coach": "business",
             "main_issue": "A", "action_step": "do A", "emotional_state": "unclear",
             "summary": "A", "coach_notes": ""},
            {"session_id": "s2", "date": "2026-01-02 10:00:00", "coach": "grief",
             "main_issue": "B", "action_step": "do B", "emotional_state": "sad",
             "summary": "B", "coach_notes": ""},
        ]
        summary_business = rc.build_memory_summary(mem, coach_filter="business")
        self.assertIn("A", summary_business)
        self.assertNotIn("B", summary_business)


class TestDetectPatterns(unittest.TestCase):
    """Regression tests for the config-schema mismatch that silently disabled
    every configured pattern (values are dicts with a 'keywords' list, not
    bare phrase lists — the old code iterated dict keys)."""

    def test_crisis_pattern_fires(self):
        found = [p.name for p in rc.detect_patterns("i want to die")]
        self.assertIn("crisis_or_high_risk", found)

    def test_config_keywords_fire(self):
        found = [p.name for p in rc.detect_patterns("i'm burnt out and it's too much")]
        self.assertIn("overwhelm", found)

    def test_schema_key_names_do_not_fire(self):
        # The old bug matched the literal strings "keywords"/"description"/etc.
        found = rc.detect_patterns("keywords description response_mode coach_action")
        high_conf = [p for p in found if p.confidence >= 0.9]
        self.assertEqual(high_conf, [])

    def test_neutral_text_matches_nothing(self):
        self.assertEqual(rc.detect_patterns("good morning, nice weather today"), [])

    def test_hardcoded_fallthroughs_still_work(self):
        found = [p.name for p in rc.detect_patterns("i should email him but i keep waiting")]
        self.assertIn("avoidance", found)

    def test_legacy_list_schema_still_accepted(self):
        # Hand-authored configs may use the old bare-list shape
        orig = rc.PATTERN_RULES
        rc.PATTERN_RULES = {"patterns": {"custom": ["magic phrase"]}}
        try:
            found = [p.name for p in rc.detect_patterns("say the magic phrase now")]
            self.assertIn("custom", found)
        finally:
            rc.PATTERN_RULES = orig

    def test_word_boundary_prevents_substring_false_positives(self):
        # "end it" inside "blend it"/"spend it" must NOT trip the crisis pattern
        for text in (
            "i got a bonus, how should i spend it?",
            "add the butter and blend it well",
            "i can't go on vacation this year",
        ):
            found = [p.name for p in rc.detect_patterns(text)]
            self.assertNotIn("crisis_or_high_risk", found, f"false positive on: {text}")

    def test_crisis_still_fires_on_real_signals(self):
        for text in ("i want to end it all", "i feel suicidal", "i can't go on anymore"):
            found = [p.name for p in rc.detect_patterns(text)]
            self.assertIn("crisis_or_high_risk", found, f"missed signal: {text}")

    def test_malformed_keywords_value_does_not_crash(self):
        orig = rc.PATTERN_RULES
        rc.PATTERN_RULES = {"patterns": {
            "bad_int": {"keywords": 5},
            "bad_str": {"keywords": "sad"},
            "bad_none": {"keywords": None},
            "good": {"keywords": ["actual phrase"]},
        }}
        try:
            found = [p.name for p in rc.detect_patterns("an actual phrase appears sad")]
            self.assertEqual(found, ["good"])  # malformed specs skipped, no crash
        finally:
            rc.PATTERN_RULES = orig


class TestMemoryCorruptionRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = rc.DATA_DIR
        rc.DATA_DIR = Path(self.tmp.name)

    def tearDown(self):
        rc.DATA_DIR = self._orig
        self.tmp.cleanup()

    def test_corrupt_file_backed_up_and_reset(self):
        bad = Path(self.tmp.name) / "brokenuser.json"
        bad.write_text('{"user_id": "brokenuser", TRUNCATED', encoding="utf-8")
        mm = rc.MemoryManager("brokenuser")  # must not raise
        self.assertEqual(mm.data["sessions"], [])
        backups = list(Path(self.tmp.name).glob("brokenuser.corrupt-*.json"))
        self.assertEqual(len(backups), 1)

    def test_atomic_save_leaves_no_tmp_files(self):
        mm = rc.MemoryManager("atomicuser")
        mm.add_session("general", "s", "i", "", "unclear", "n")
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])
        # And the file round-trips
        mm2 = rc.MemoryManager("atomicuser")
        self.assertEqual(len(mm2.data["sessions"]), 1)


class _EngineHarness(unittest.TestCase):
    """Shared scaffolding: real coach, fake index, temp memory."""

    def setUp(self):
        class FakeIndex:
            chunks = []
            def retrieve(self, query, k=3, min_score=0.35):
                return []
        coaches = rc.load_coaches()
        self.coach = coaches.get("general") or next(iter(coaches.values()))
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = rc.DATA_DIR
        rc.DATA_DIR = Path(self.tmp.name)
        self.mm = rc.MemoryManager("harness")
        self.engine = rc.CoachEngine(self.mm, self.coach, FakeIndex())

    def tearDown(self):
        rc.DATA_DIR = self._orig_dir
        self.tmp.cleanup()


class TestCrisisHandoff(_EngineHarness):
    def test_fallback_returns_crisis_resources(self):
        text, patterns = self.engine.fallback_response("i want to die")
        self.assertIn("crisis_or_high_risk", [p.name for p in patterns])
        # Must hand off to human support, not coach through it
        self.assertIn("findahelpline.com", text)
        self.assertIn("116 123", text)

    def test_normal_text_gets_normal_fallback(self):
        text, _ = self.engine.fallback_response("i keep putting off my taxes")
        self.assertNotIn("findahelpline.com", text)

    def test_crisis_not_recorded_as_recurring_block(self):
        self.engine._update_memory(
            "i want to die", "response",
            rc.detect_patterns("i want to die"),
        )
        blocks = [b["pattern"] for b in self.mm.data["patterns"]["recurring_blocks"]]
        self.assertNotIn("crisis_or_high_risk", blocks)


class TestPartialEmbedCacheKept(unittest.TestCase):
    """A retry while the embed server is still down must not wipe a richer
    partial cache from a previous attempt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        coach_dir = Path(self.tmp.name) / "testcoach"
        (coach_dir / "sources").mkdir(parents=True)
        # 3 long paragraphs (~420 chars each) → 3 chunks at the default
        # 500-char target (short paragraphs would coalesce into one chunk)
        (coach_dir / "sources" / "doc.md").write_text(
            "\n\n".join(f"Paragraph {i}. " + ("content words here " * 21) for i in range(3)),
            encoding="utf-8",
        )
        self.coach = rc.Coach(
            name="testcoach", display_name="T", description="", model="m",
            system_prompt="s", dir=coach_dir,
        )
        self._orig_embed = rc.ollama_embed

    def tearDown(self):
        rc.ollama_embed = self._orig_embed
        self.tmp.cleanup()

    def test_retry_keeps_richer_partial(self):
        calls = {"n": 0}
        def embed_first_only(text):
            calls["n"] += 1
            return [1.0, 0.0] if calls["n"] <= 2 else None  # probe + 1 chunk
        rc.ollama_embed = embed_first_only
        idx = rc.SourceIndex(self.coach)
        idx.reload()
        cache1 = json.loads(self.coach.embeddings_cache.read_text(encoding="utf-8"))
        entry1 = cache1["files"]["doc.md"]
        self.assertTrue(entry1.get("incomplete"))
        kept = len(entry1["chunks"])
        self.assertGreaterEqual(kept, 1)

        # Second reload: server fully down — every embed fails
        rc.ollama_embed = lambda text: None
        idx2 = rc.SourceIndex(self.coach)
        idx2.reload()
        cache2 = json.loads(self.coach.embeddings_cache.read_text(encoding="utf-8"))
        entry2 = cache2["files"]["doc.md"]
        self.assertTrue(entry2.get("incomplete"))
        self.assertEqual(len(entry2["chunks"]), kept)  # partials preserved


class TestRegenerateSkipsMemory(_EngineHarness):
    def setUp(self):
        super().setUp()
        self._orig_stream = rc.llama_chat_stream
        def fake_stream(messages, model):
            yield "hello "
            yield "there"
        rc.llama_chat_stream = fake_stream

    def tearDown(self):
        rc.llama_chat_stream = self._orig_stream
        super().tearDown()

    def test_update_memory_true_records_session(self):
        list(self.engine.respond_stream("first message", update_memory=True))
        self.assertEqual(len(self.mm.data["sessions"]), 1)

    def test_update_memory_false_skips_session(self):
        list(self.engine.respond_stream("regen message", update_memory=False))
        self.assertEqual(len(self.mm.data["sessions"]), 0)


class TestBuildMessagesHistory(unittest.TestCase):
    """Tests for multi-turn conversation history handling in CoachEngine.build_messages."""

    def setUp(self):
        # Minimal fake source index so CoachEngine doesn't try to reach Ollama
        class FakeIndex:
            chunks = []
            def retrieve(self, query, k=3, min_score=0.35):
                return []
        self.fake_index = FakeIndex()

        # Load a real coach for the rest of the scaffolding
        coaches = rc.load_coaches()
        self.coach = coaches.get("general") or next(iter(coaches.values()))

        # Temp memory
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = rc.DATA_DIR
        rc.DATA_DIR = Path(self.tmp.name)
        self.mm = rc.MemoryManager("histtest")
        self.engine = rc.CoachEngine(self.mm, self.coach, self.fake_index)

    def tearDown(self):
        rc.DATA_DIR = self._orig
        self.tmp.cleanup()

    def test_no_history_behaves_like_before(self):
        msgs = self.engine.build_messages("hello")
        roles = [m["role"] for m in msgs]
        # Only systems + the final user message
        self.assertEqual(roles[-1], "user")
        self.assertEqual(msgs[-1]["content"], "hello")
        self.assertNotIn("assistant", roles)

    def test_history_appended_before_current_message(self):
        history = [
            {"role": "user", "content": "I'm overwhelmed"},
            {"role": "assistant", "content": "What's the biggest thing?"},
        ]
        msgs = self.engine.build_messages("the launch", history=history)
        # last three messages must be: user(prev), assistant(prev), user(current)
        self.assertEqual(msgs[-3], {"role": "user", "content": "I'm overwhelmed"})
        self.assertEqual(msgs[-2], {"role": "assistant", "content": "What's the biggest thing?"})
        self.assertEqual(msgs[-1], {"role": "user", "content": "the launch"})

    def test_history_role_coach_mapped_to_assistant(self):
        """UI uses 'coach' as a role name; backend should map it to 'assistant'."""
        history = [
            {"role": "user", "content": "hi"},
            {"role": "coach", "content": "hello back"},
        ]
        msgs = self.engine.build_messages("next thing", history=history)
        assistants = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["content"], "hello back")

    def test_history_bad_shapes_are_dropped_not_raised(self):
        history = [
            None,
            "not a dict",
            {"role": "user"},  # no content
            {"role": "system", "content": "should not be forwarded"},
            {"role": "user", "content": ""},  # empty content
            {"role": "user", "content": "real user msg"},
        ]
        msgs = self.engine.build_messages("now", history=history)
        hist_msgs = [m for m in msgs if m["role"] in ("user", "assistant") and m["content"] != "now"]
        # Only the one valid entry should survive
        self.assertEqual(len(hist_msgs), 1)
        self.assertEqual(hist_msgs[0], {"role": "user", "content": "real user msg"})

    def test_history_capped_at_max(self):
        # 30 turns — far over the 12-message cap
        history = []
        for i in range(30):
            history.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"})
        msgs = self.engine.build_messages("final", history=history)
        hist_msgs = [m for m in msgs if m["role"] in ("user", "assistant") and m["content"] != "final"]
        self.assertEqual(len(hist_msgs), 12)
        # Should be the most recent 12 (m18..m29)
        self.assertEqual(hist_msgs[0]["content"], "m18")
        self.assertEqual(hist_msgs[-1]["content"], "m29")


class TestModelOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_path = rc.SETTINGS_PATH
        self._orig_settings = dict(rc._SETTINGS)
        rc.SETTINGS_PATH = Path(self.tmp.name) / "settings.json"
        rc._SETTINGS.clear()

    def tearDown(self):
        rc.SETTINGS_PATH = self._orig_path
        rc._SETTINGS.clear()
        rc._SETTINGS.update(self._orig_settings)
        self.tmp.cleanup()

    def test_set_get_clear_roundtrip(self):
        self.assertIsNone(rc.get_model_override())
        rc.set_model_override("qwen3:8b")
        self.assertEqual(rc.get_model_override(), "qwen3:8b")
        saved = json.loads(rc.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["model_override"], "qwen3:8b")
        rc.set_model_override(None)
        self.assertIsNone(rc.get_model_override())

    def test_effective_model_prefers_override(self):
        coach = rc.Coach(name="x", display_name="X", description="", model="llama3.1",
                         system_prompt="s", dir=Path("."))
        self.assertEqual(rc.effective_model(coach), "llama3.1")
        rc.set_model_override("mistral")
        self.assertEqual(rc.effective_model(coach), "mistral")


class TestPdfSupport(unittest.TestCase):
    def test_read_source_text_plain(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "note.md"
            p.write_text("hello source", encoding="utf-8")
            self.assertEqual(rc.read_source_text(p), "hello source")

    @unittest.skipUnless(rc.PDF_SUPPORT, "pypdf not installed")
    def test_pdf_extension_in_supported_ext(self):
        self.assertIn(".pdf", rc.SourceIndex.SUPPORTED_EXT)

    @unittest.skipUnless(rc.PDF_SUPPORT, "pypdf not installed")
    def test_read_source_text_pdf(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "doc.pdf"
            w = PdfWriter()
            w.add_blank_page(width=200, height=200)
            with p.open("wb") as f:
                w.write(f)
            # Blank page → no text → None (unreadable-or-empty contract)
            self.assertIsNone(rc.read_source_text(p))

    def test_corrupt_pdf_returns_none(self):
        if not rc.PDF_SUPPORT:
            self.skipTest("pypdf not installed")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.pdf"
            p.write_bytes(b"not a real pdf")
            self.assertIsNone(rc.read_source_text(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
