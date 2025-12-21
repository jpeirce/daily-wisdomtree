import unittest
import sys
import os

# Add scripts to path so we can import
sys.path.append(os.path.join(os.getcwd(), 'scripts'))
from fetch_and_summarize import clean_llm_output

class TestCleaner(unittest.TestCase):

    def test_clean_banned_words(self):
        dirty_text = "The smart money is buying the dip while whales sell."
        clean_text = clean_llm_output(dirty_text)
        
        self.assertNotIn("smart money", clean_text.lower())
        self.assertNotIn("whales", clean_text.lower())
        self.assertIn("market participants", clean_text)
        self.assertIn("Language normalization applied", clean_text)

    def test_clean_markdown_ticks(self):
        md_text = "```markdown\n# Title\nContent\n```"
        clean_text = clean_llm_output(md_text)
        self.assertEqual(clean_text, "# Title\nContent")
        # Should not append note if no banned words
        self.assertNotIn("Language normalization applied", clean_text)

    def test_clean_institutions(self):
        text = "Institutions are adding shorts."
        clean_text = clean_llm_output(text)
        self.assertNotIn("Institutions", clean_text)
        self.assertIn("market participants", clean_text)

if __name__ == '__main__':
    unittest.main()

