import unittest

from cdmw.modding.static_mesh_replacer import infer_static_replacement_part_role


class StaticMeshPartRoleTests(unittest.TestCase):
    def test_character_part_names_get_specific_roles(self) -> None:
        cases = {
            "CD_PHW_00_Head_00_0001_01": "head/face",
            "CD_PHW_00_Nude_0001_hand": "hand/arm",
            "CD_PHW_00_Nude_0001": "body",
            "CD_PHM_00_Hair_0007": "hair",
            "CD_PHW_00_Boots_0001": "foot/leg",
        }

        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(infer_static_replacement_part_role(label), expected)

    def test_weapon_part_names_keep_existing_roles(self) -> None:
        cases = {
            "CD_PHM_02_Sword_Blade_0017": "blade",
            "CD_PHM_01_Handle_0034_mg": "handle",
            "CD_PHM_01_Guard_0026": "guard",
            "spike_trim_detail": "accessory/detail",
        }

        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(infer_static_replacement_part_role(label), expected)


if __name__ == "__main__":
    unittest.main()
