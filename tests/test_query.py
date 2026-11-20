import unittest

from herdr_zshz.query import match_spans, matches, parse, score_match


class ParseTest(unittest.TestCase):
    def test_and_clauses(self):
        self.assertEqual(len(parse("foo bar")), 2)

    def test_or_alternatives(self):
        groups = parse("foo | bar baz")
        self.assertEqual([[term.text for term in group] for group in groups], [["foo", "bar"], ["baz"]])
        self.assertEqual(len(parse("a|b")[0]), 2)

    def test_modifiers(self):
        term = parse("!^prefix$")[0][0]
        self.assertTrue(term.negated)
        self.assertTrue(term.prefix)
        self.assertTrue(term.suffix)

    def test_empty_query_matches(self):
        self.assertTrue(matches("", "anything"))


class MatchTest(unittest.TestCase):
    def test_substring(self):
        self.assertTrue(matches("roj", "project"))
        self.assertFalse(matches("xyz", "project"))

    def test_smartcase(self):
        self.assertTrue(matches("proj", "Project"))
        self.assertFalse(matches("Proj", "project"))

    def test_or_clause(self):
        self.assertTrue(matches("foo | bar baz", "bar bazooka"))
        self.assertFalse(matches("foo | bar baz", "foo qux"))

    def test_negation(self):
        self.assertTrue(matches("!test", "project"))
        self.assertFalse(matches("!test", "test-project"))
        self.assertFalse(matches("!^test", "test-project"))
        self.assertTrue(matches("!suffix$", "suffixes"))

    def test_anchors(self):
        self.assertTrue(matches("^pro", "project"))
        self.assertFalse(matches("^roj", "project"))
        self.assertTrue(matches("ject$", "project"))
        self.assertFalse(matches("proj$", "project"))

    def test_combined_anchors_are_exact(self):
        self.assertTrue(matches("^foo$", "foo"))
        self.assertFalse(matches("^foo$", "foobar"))
        self.assertFalse(matches("^foo$", "barfoo"))
        self.assertFalse(matches("^foo$", "foo bar"))
        self.assertEqual(match_spans("^foo$", "foo"), [(0, 3)])
        self.assertIsNone(match_spans("^foo$", "foobar"))

    def test_negated_combined_anchor(self):
        self.assertTrue(matches("!^foo$", "foobar"))
        self.assertFalse(matches("!^foo$", "foo"))
        self.assertTrue(matches("!^foo$", "foo bar"))

    def test_or_alternatives_keep_their_own_negation(self):
        # "!foo|bar" is (NOT foo) OR (bar), not NOT (foo OR bar).
        self.assertTrue(matches("!foo|bar", "foobar"))
        self.assertTrue(matches("!foo|bar", "qux"))
        self.assertTrue(matches("!foo|bar", "bar"))
        self.assertFalse(matches("^foo|bar$", "zzz"))
        self.assertFalse(matches("!qux|^bar", "qux"))
        self.assertTrue(matches("!qux|^bar", "barbecue"))
        self.assertEqual(match_spans("!foo|bar", "qux"), [])
        self.assertEqual(match_spans("!foo|bar", "barbecue"), [(0, 3)])

    def test_quote_is_substring(self):
        self.assertTrue(matches("'roj", "project"))

    def test_spans(self):
        self.assertEqual(match_spans("roj", "project"), [(1, 4)])
        self.assertEqual(match_spans("^pro", "project"), [(0, 3)])
        self.assertEqual(match_spans("ject$", "project"), [(3, 7)])
        self.assertIsNone(match_spans("nope", "project"))

    def test_spans_are_per_clause(self):
        self.assertEqual(match_spans("pro ject", "project"), [(0, 3), (3, 7)])

    def test_score_match_rates_open_higher_most_of_the_time(self):
        # Open basename match beats closed exact basename match.
        open_direct = score_match(
            match_spans("raffi", "~/git/raffi"), "~/git/raffi", is_open=True
        )
        closed_exact = score_match(
            match_spans("raffi", "~/git/raffi"), "~/git/raffi", is_open=False
        )
        self.assertGreater(open_direct, closed_exact)

        # Open weak path/tab match beats ordinary closed non-exact match...
        open_weak = score_match(
            match_spans("api", "~/api/frontend"), "~/api/frontend", is_open=True
        )
        closed_sub = score_match(
            match_spans("api", "~/git/my-api"), "~/git/my-api", is_open=False
        )
        self.assertGreater(open_weak, closed_sub)

        # ...but a closed exact basename match beats a weak open path/tab match.
        closed_exact_api = score_match(
            match_spans("api", "~/git/api"), "~/git/api", is_open=False
        )
        self.assertGreater(closed_exact_api, open_weak)


if __name__ == "__main__":
    unittest.main()
