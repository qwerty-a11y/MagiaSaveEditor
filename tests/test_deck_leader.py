"""Quest-team edits must leave a leader that the game can start a battle with."""
import unittest
from types import SimpleNamespace

from magiasave.model import Editor


class DeckLeaderTest(unittest.TestCase):
    def setUp(self):
        deck = {"deckType": 11, "questPositionHelper": 3, "questPositionId1": 5,
                "userCardId1": "a", "questEpisodeUserCardId": "a"}
        self.sibling = {"deckType": 21, "userCardId1": "b", "questEpisodeUserCardId": "b"}
        state = {"userDeckList": [deck, self.sibling],
                 "userCardList": [{"id": x} for x in ("a", "b", "c")]}
        profile = {"formatVersion": 1, "kind": "full-unlock", "state": state}
        self.editor = Editor(SimpleNamespace(template="full", profile=profile),
                             catalog=object(), masters=object())

    @property
    def deck(self):
        return self.editor.state["userDeckList"][0]

    def edit(self, op, slot, **fields):
        self.editor.apply([dict(op="deck." + op, deckType=11, slot=slot, **fields)])
        self.assertEqual(self.editor.state["userDeckList"][1], self.sibling)

    def test_replace_only_member(self):
        self.edit("member", 1, userCardId="b")
        self.assertEqual(self.deck["questEpisodeUserCardId"], "b")
        self.assertEqual(self.deck["questPositionHelper"], 3)

    def test_keep_valid_leader_in_another_slot(self):
        self.deck.update(userCardId2="b", questPositionId2=9, questEpisodeUserCardId="b")
        self.edit("member", 1, userCardId="c")
        self.assertEqual(self.deck["questEpisodeUserCardId"], "b")

    def test_remove_selects_the_first_active_member_not_the_lowest_grid_position(self):
        self.deck.update(userCardId2="b", questPositionId2=9, userCardId3="c", questPositionId3=1)
        self.edit("removeMember", 1)
        self.assertEqual(self.deck["questEpisodeUserCardId"], "b")

    def test_removing_only_active_member_leaves_no_leader(self):
        self.deck.update(userCardId2="b", questPositionId2=10)
        self.edit("removeMember", 1)
        self.assertNotIn("questEpisodeUserCardId", self.deck)


if __name__ == "__main__":
    unittest.main()
