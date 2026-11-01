"""Exercise every public world option, finite interactions, bounds and map resources.

Numeric values and resource quantities have unbounded combinations. Test their
boundary/type classes, every enum value, all gameplay boolean combinations, and
all resource-field combinations rather than claiming an exhaustive Cartesian test.
"""
import configparser
import copy
from functools import lru_cache
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CUSTOM = '/Script/DuneSandbox.UserServerCustomSettings'
# Independent expectations: Helm values -> actual game keys and upstream ranges.
NUMBERS = {
    'gatheringMultiplier': ('GatheringAmount', .1, 10),
    'xpMultiplier': ('GlobalXpMultiplier', 0, 10),
    'combatXpMultiplier': ('CombatXp', 0, 10),
    'gatheringXpMultiplier': ('GatheringXp', 0, 10),
    'missionXpMultiplier': ('MissionXp', 0, 10),
    'craftingTimeMultiplier': ('CraftingTimeMultiplier', 0, 5),
    'craftingCostMultiplier': ('CraftingCost', 0, 10),
    'buildingCostMultiplier': ('BuildingCostMultiplier', 0, 10),
    'inventoryVolumeMultiplier': ('InventoryVolumeMultiplier', .1, 10),
    'lootRespawnSpeed': ('LootRespawnSpeed', .1, 10),
    'resourceRespawnSpeed': ('ResourceRespawnSpeed', .1, 10),
    'itemDurabilityDrainMultiplier': ('ItemDurabilityDrainMultiplier', 0, 10),
    'vehicleDurabilityDamageMultiplier': ('dw.VehicleDurabilityDamageMultiplier', 0, 10),
    'buildingPieceLimitMultiplier': ('BuildingPieceLimitMultiplier', .1, 10),
    'thirstMultiplier': ('ThirstMultiplier', 0, 10),
    'heatBuildupMultiplier': ('HeatBuildupRate', 0, 10),
}
BOOLS = {
    'maxDurabilityLossEnabled': ('bEnableItemMaxDurabilityLoss', True),
    'sandwormsEnabled': ('bAllowSandworms', True),
    'sandstormsEnabled': ('bAllowSandstorms', True),
    'coriolisStormsEnabled': ('m_bCoriolisAutoSpawnEnabled', True),
    'infiniteBuildingStability': ('bBuildingInfiniteStability', False),
    'dynamicBuildingDamageEnabled': ('bAllowDynamicBuildingDamage', True),
}
ENUMS = {
    'equipmentDropOnDeath': ('DropEquipmentOnDeath', ('All', 'Backpack', 'Default', 'None'), 'Default'),
    'sandwormDeathPenalty': ('SandwormConsequences', ('All', 'Backpack', 'Default', 'None'), 'All'),
    'playerDeathLootRule': ('PlayerDeathLootRule', ('DependsOnSecurityZone', 'NeverAllowOtherPlayers', 'AlwaysAllowOtherPlayers'), 'DependsOnSecurityZone'),
}
LARGE_MAPS = {
    'Survival_1': '12Gi', 'DeepDesert_1': '15Gi',
    'DLC_Story_LostHarvest_EcolabA': '3Gi', 'CB_Dungeon_Hephaestus': '3Gi',
    'CB_Dungeon_OldCarthag': '3Gi', 'Story_Faction_Outpost_Atre': '3Gi',
    'Story_Faction_Outpost_Hark': '3Gi', 'Story_HeighlinerDungeon': '3Gi',
    'CB_Ecolab_Bronze_Green_089': '6Gi', 'CB_Ecolab_Bronze_Green_152': '3Gi',
    'CB_Ecolab_Bronze_Green_024': '3Gi', 'CB_Ecolab_Bronze_Green_195': '3Gi',
    'CB_Ecolab_Bronze_Green_136': '3Gi', 'CB_Overland_M_01': '3Gi',
    'CB_Overland_S_04': '3Gi', 'CB_Overland_S_06': '3Gi',
}


@lru_cache(maxsize=8)
def render(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'values.yaml'
        path.write_text(text)
        return subprocess.run(
            ['helm', 'template', 'test', str(ROOT), '--namespace', 'custom-game',
             '--show-only', 'templates/configmaps.yaml', '-f', str(path)],
            capture_output=True, text=True,
        )


@unittest.skipUnless(shutil.which('helm'), 'Helm is required for world setting checks')
class WorldSettingsTests(unittest.TestCase):
    def world(self, values):
        # JSON is valid YAML and quotes strings consistently across PyYAML/Helm,
        # including scientific-notation resource quantities such as "1e9".
        result = render(json.dumps({'world': values}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('{{', result.stdout, 'Unresolved Helm expressions')
        template = next(obj for obj in yaml.safe_load_all(result.stdout)
                        if obj and obj['kind'] == 'ConfigMap'
                        and obj['metadata']['name'] == 'dune-battlegroup-template')
        return yaml.safe_load(template['data']['battlegroup.yaml'])

    def configs(self, values):
        world = self.world(values)
        files = world['spec']['serverGroup']['template']['spec']['global']['userIniConfig']['files']
        configs = {}
        for name, content in files.items():
            # Unreal INI arrays intentionally repeat keys; array contents are checked raw.
            config = configparser.ConfigParser(interpolation=None, strict=False)
            config.optionxform = str
            config.read_string(content)
            configs[name] = config
        return configs

    def custom(self, configs):
        return configs['UserServerCustomSettings.ini'][CUSTOM]

    def assert_rejected(self, world):
        result = render(json.dumps({'world': world}))
        self.assertNotEqual(result.returncode, 0, result.stdout[:200])
        self.assertIn('world', result.stderr)

    def assert_gameplay(self, values, **world):
        configs = self.configs(dict(world, gameplay=values))
        custom = self.custom(configs)
        engine = configs['UserEngine.ini']['ConsoleVariables']
        for name, value in values.items():
            if name in NUMBERS:
                key = NUMBERS[name][0]
                section = engine if name == 'vehicleDurabilityDamageMultiplier' else custom
                self.assertEqual(float(section[key]), value, name)
            elif name in BOOLS:
                key = BOOLS[name][0]
                section = (configs['UserGame.ini']['/Script/DuneSandbox.SandStormConfig']
                           if name == 'coriolisStormsEnabled' else custom)
                self.assertEqual(section.getboolean(key), value, name)
            elif name in ENUMS:
                self.assertEqual(custom[ENUMS[name][0]], value, name)
            elif name == 'maxBases':
                if value is None:
                    self.assertNotIn('FiefdomLimit', custom)
                else:
                    self.assertEqual(custom.getint('FiefdomLimit'), value)
        if 'gatheringMultiplier' in values:
            for key in ('Dune.GlobalMiningOutputMultiplier', 'Dune.GlobalVehicleMiningOutputMultiplier'):
                self.assertEqual(float(engine[key]), values['gatheringMultiplier'])
        if 'sandstormsEnabled' in values:
            for key in ('Sandstorm.Enabled', 'Sandstorm.Treasure.Enabled'):
                self.assertEqual(engine.getint(key), int(values['sandstormsEnabled']))
        if 'sandwormsEnabled' in values:
            self.assertEqual(engine.getint('sandworm.dune.Enabled'), int(values['sandwormsEnabled']))
            self.assertEqual(engine.getboolean('Sandworm.SandwormDangerZonesEnabled'), values['sandwormsEnabled'])
        self.assertEqual(engine['Bgd.ServerLoginPassword'], '""')
        return configs

    def test_taxation_defaults_and_overrides(self):
        defaults = yaml.safe_load((ROOT / 'values.yaml').read_text())['world']['taxation']
        for values in (defaults, dict(enabled=True, cycleLengthSeconds=604800,
                                     paymentItemPerHour=0, spicePerHour=2.5,
                                     costMultiplierPerLandclaim=[0, 0, 0.25],
                                     costMultiplierPerVerticalExtension=[1, 1])):
            with self.subTest(values=values):
                world = self.world({'taxation': values})
                text = world['spec']['serverGroup']['template']['spec']['global']['userIniConfig']['files']['UserGame.ini']
                section = self.configs({'taxation': values})['UserGame.ini']['/Script/DuneSandbox.TaxationSettings']
                self.assertEqual(section.getboolean('m_bTaxationEnabled'), values['enabled'])
                self.assertEqual(section.getint('m_TaxationCycleLengthSeconds'), values['cycleLengthSeconds'])
                self.assertEqual(section.getfloat('m_PaymentItemPerHour'), values['paymentItemPerHour'])
                self.assertEqual(section.getfloat('m_SpicePerHour'), values['spicePerHour'])
                for key, ini in [('costMultiplierPerLandclaim', 'm_CostMultiplierPerLandclaim'),
                                 ('costMultiplierPerVerticalExtension', 'm_CostMultiplierPerVerticalExtension')]:
                    self.assertIn('!' + ini + '=ClearArray', text)
                    entries = [float(line.split('=', 1)[1]) for line in text.splitlines()
                               if line.startswith('.' + ini + '=')]
                    self.assertEqual(entries, values[key])

    def test_invalid_taxation_settings(self):
        for key, values in {
            'enabled': ['true', 1, None],
            'cycleLengthSeconds': [0, -1, 1.5, '60', None],
            'paymentItemPerHour': [-1, '1', None],
            'spicePerHour': [-1, '1', None],
            'costMultiplierPerLandclaim': [[], [-1], ['1'], None],
            'costMultiplierPerVerticalExtension': [[], [-1], [True], None],
            'typo': [1],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assert_rejected({'taxation': {key: value}})

    def test_every_public_gameplay_option_has_render_expectations(self):
        defaults = yaml.safe_load((ROOT / 'values.yaml').read_text())['world']['gameplay']
        self.assertEqual(set(defaults), set(NUMBERS) | set(BOOLS) | set(ENUMS) | {'maxBases'})
        self.assert_gameplay(defaults)
        custom = self.custom(self.configs({}))
        self.assertEqual(custom['DifficultyLevel'], 'Custom')
        for key in ('WaterExtractionRate', 'FuelBurnTimeMultiplier', 'PlayerDamageToPlayer',
                    'PlayerDamageToNPC', 'PlayerDamageToVehicle', 'NPCHealth', 'NPCDamageToPlayer',
                    'NPCDamageToNPC', 'NPCRespawnMultiplier', 'PVPDamageStructures',
                    'PlayerStaminaDrain', 'IntelPointsGainMultiplier', 'PlayerShieldDamageAbsorptionMultiplier',
                    'NPCShieldDamageAbsorptionMultiplier', 'LandsraadContributionMultiplier',
                    'LandsraadSpecializationXpMultiplier', 'LandsraadFactionStandingMultiplier'):
            self.assertEqual(float(custom[key]), 1, key)
        self.assertTrue(custom.getboolean('bIsBuildingRestrictionsEnabled'))
        self.assertEqual(float(custom['BaseBackupToolTimeRestriction']), 16)

    def test_all_numeric_bounds_and_fractions(self):
        for position in (1, 2):
            values = {name: spec[position] for name, spec in NUMBERS.items()}
            with self.subTest(position=position):
                self.assert_gameplay(values)
        self.assert_gameplay({name: .75 for name in NUMBERS})
        # Each value also works alone, without changing other defaults.
        for name in NUMBERS:
            with self.subTest(name=name):
                configs = self.assert_gameplay({name: 2.5})
                defaults = yaml.safe_load((ROOT / 'values.yaml').read_text())['world']['gameplay']
                for other, (key, _, _) in NUMBERS.items():
                    if other == name or other == 'vehicleDurabilityDamageMultiplier':
                        continue
                    self.assertEqual(float(self.custom(configs)[key]), defaults[other])

    def test_all_gameplay_boolean_combinations(self):
        for index, flags in enumerate(itertools.product((False, True), repeat=len(BOOLS))):
            values = dict(zip(BOOLS, flags))
            with self.subTest(values=values):
                self.assert_gameplay(values, pvpMode=('NoPVP', 'Limited', 'FullPVP')[index % 3],
                                     battlEyeEnabled=(None, True, False)[index // 3 % 3])

    def test_all_death_rule_combinations(self):
        for choices in itertools.product(*(spec[1] for spec in ENUMS.values())):
            values = dict(zip(ENUMS, choices))
            with self.subTest(values=values):
                self.assert_gameplay(values)

    def test_all_battleye_and_pvp_combinations(self):
        for enabled, mode in itertools.product((None, True, False), ('NoPVP', 'Limited', 'FullPVP')):
            with self.subTest(enabled=enabled, mode=mode):
                configs = self.configs({'battlEyeEnabled': enabled, 'pvpMode': mode})
                engine = configs['UserEngine.ini']['ConsoleVariables']
                if enabled is None:
                    self.assertNotIn('BattlEye.Enabled', engine)
                else:
                    self.assertEqual(engine.getboolean('BattlEye.Enabled'), enabled)
                self.assertEqual(self.custom(configs)['PVPMode'], mode)

    def test_all_max_base_counts_and_null(self):
        for count in (None, *range(11)):
            with self.subTest(count=count):
                self.assert_gameplay({'maxBases': count})

    def test_numeric_options_reject_invalid_values(self):
        for name, (_, low, high) in NUMBERS.items():
            for value in (low - .01, high + .01, '1', True, None, [], {}):
                with self.subTest(name=name, value=value):
                    self.assert_rejected({'gameplay': {name: value}})

    def test_boolean_and_enum_options_reject_invalid_values(self):
        for name in BOOLS:
            for value in ('false', 0, None, [], {}):
                with self.subTest(name=name, value=value):
                    self.assert_rejected({'gameplay': {name: value}})
        for name in ENUMS:
            for value in ('invalid', '', 0, False, None, [], {}):
                with self.subTest(name=name, value=value):
                    self.assert_rejected({'gameplay': {name: value}})
        for value in (-1, 11, 1.5, True, '3', [], {}):
            with self.subTest(maxBases=value):
                self.assert_rejected({'gameplay': {'maxBases': value}})
        for world in ({'battlEyeEnabled': 'false'}, {'pvpMode': 'PvE'}, {'pvpMode': None},
                      {'gameplay': {'typo': 1}}, {'gameplay': None}, {'gameplay': []},
                      {'mapResources': None}, {'mapResources': []}):
            with self.subTest(world=world):
                self.assert_rejected(world)

    def maps(self, values):
        world = self.world(values)
        return {item['map']: item['resources']
                for item in world['spec']['serverGroup']['template']['spec']['sets']}

    def test_default_resources_for_every_map(self):
        maps = self.maps({})
        self.assertEqual(len(maps), 35)
        self.assertTrue(set(LARGE_MAPS) <= set(maps))
        for name, resources in maps.items():
            self.assertEqual(resources, {'limits': {'memory': LARGE_MAPS.get(name, '2Gi')}})

    def test_all_resource_fields_on_every_map(self):
        defaults = self.maps({})
        fields = (('requests', 'cpu', '500m'), ('requests', 'memory', '1Gi'),
                  ('limits', 'cpu', '2'), ('limits', 'memory', '20Gi'))
        for flags in itertools.product((False, True), repeat=len(fields)):
            overrides = {}
            for enabled, (kind, resource, value) in zip(flags, fields):
                if enabled:
                    overrides.setdefault(kind, {})[resource] = value
            with self.subTest(flags=flags):
                maps = self.maps({'mapResources': {name: overrides for name in defaults}})
                for name in defaults:
                    expected = copy.deepcopy(defaults[name])
                    for kind, resources in overrides.items():
                        expected.setdefault(kind, {}).update(resources)
                    self.assertEqual(maps[name], expected, name)

    def test_map_overrides_do_not_leak_between_maps(self):
        defaults = self.maps({})
        overrides = {name: {'requests': {'cpu': f'{index + 1}m'},
                            'limits': {'memory': f'{index + 20}Gi'}}
                     for index, name in enumerate(defaults)}
        maps = self.maps({'mapResources': overrides})
        for name in defaults:
            self.assertEqual(maps[name], overrides[name])
        maps = self.maps({'mapResources': {'Survival_1': {'limits': {'cpu': '4'}}}})
        for name in defaults:
            expected = copy.deepcopy(defaults[name])
            if name == 'Survival_1':
                expected['limits']['cpu'] = '4'
            self.assertEqual(maps[name], expected)

    def test_resource_quantity_formats(self):
        for cpu, memory in (('0', '0'), ('0.125', '0.5Gi'), ('125m', '500M'),
                            ('4', '1073741824'), ('1.001', '1e9'), ('2', '1E+9'),
                            ('2', '.5Gi'), ('2', '1Ki'), ('2', '1Ti')):
            with self.subTest(cpu=cpu, memory=memory):
                override = {'requests': {'cpu': cpu, 'memory': memory}}
                maps = self.maps({'mapResources': {'Survival_1': override}})
                self.assertEqual(maps['Survival_1']['requests'], override['requests'])

    def test_invalid_map_resources_are_rejected(self):
        for override in ({'Typo': {}}, {'Survival_1': {'gpu': '1'}},
                         {'Survival_1': {'limits': {'storage': '1Gi'}}},
                         {'Survival_1': {'requests': []}}, {'Survival_1': []}):
            with self.subTest(override=override):
                self.assert_rejected({'mapResources': override})
        for field, values in {
            'cpu': ('-1', '1Gi', '500mm', '', '1.0001', '1e3', 1, True, None),
            'memory': ('-1Gi', '1GB', '1K', 'gi', 'NaN', 'Inf', '', '1Gi\nInjected=True', 1, True, None),
        }.items():
            for kind in ('requests', 'limits'):
                for value in values:
                    with self.subTest(kind=kind, field=field, value=value):
                        self.assert_rejected({'mapResources': {'Survival_1': {kind: {field: value}}}})

    def test_title_stop_and_settings_work_together(self):
        values = {'title': 'My "Dune": world', 'stop': True, 'battlEyeEnabled': False,
                  'pvpMode': 'NoPVP', 'gameplay': {'maxBases': 5, 'gatheringMultiplier': 2,
                  'sandwormsEnabled': False, 'equipmentDropOnDeath': 'None'},
                  'mapResources': {'Survival_1': {'requests': {'cpu': '500m', 'memory': '8Gi'}}}}
        world = self.world(values)
        self.assertEqual(world['spec']['title'], values['title'])
        self.assertTrue(world['spec']['stop'])
        self.assertEqual(world['metadata']['name'], 'dune-world')
        configs = self.configs(values)
        self.assertEqual(self.custom(configs)['DropEquipmentOnDeath'], 'None')
        self.assertEqual(self.custom(configs).getint('FiefdomLimit'), 5)
