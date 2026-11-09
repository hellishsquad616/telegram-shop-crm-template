import unittest

from analytics_helpers import gender_counts, infer_gender


class AnalyticsHelpersTest(unittest.TestCase):
    def test_gender_by_name_anywhere_in_fio(self) -> None:
        self.assertEqual(infer_gender("Иванов Иван Сергеевич"), "male")
        self.assertEqual(infer_gender("Анна Петрова"), "female")
        self.assertEqual(infer_gender("Компания Ромашка"), "unknown")

    def test_gender_counts_keep_unknown(self) -> None:
        self.assertEqual(
            gender_counts(["Иван", "Мария", "Неизвестно"]),
            {"male": 1, "female": 1, "unknown": 1},
        )


if __name__ == "__main__":
    unittest.main()
