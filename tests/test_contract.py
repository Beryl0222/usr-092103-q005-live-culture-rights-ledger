import json
import unittest
from pathlib import Path

from src.validator import validate_event

ROOT = Path(__file__).parents[1]

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    @unittest.skipUnless(HAS_JSONSCHEMA, "未安装 jsonschema，跳过正式契约校验")
    def test_all_events_match_json_schema(self) -> None:
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)

        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        validator.validate(sample)  # 样例本身必须满足正式 schema

        scenario = ROOT / "data" / "livestream_scenario.jsonl"
        if not scenario.exists():
            self.skipTest("先运行 python3 -m src.demo 生成故事线日志")
        for lineno, line in enumerate(scenario.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            event = json.loads(line)
            errors = list(validator.iter_errors(event))
            if errors:
                self.fail(f"第 {lineno} 行违反 schema：{errors[0].message}")


if __name__ == "__main__":
    unittest.main()
