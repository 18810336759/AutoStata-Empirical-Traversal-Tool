"""Read-only preflight for AUTOstata 2.0. No fitting, filtering or output writes.

Sample descriptions are metadata. Validation never infers research decisions.
Use the CLI to check all modules, including inactive examples, before a run.
"""
from __future__ import annotations

import argparse
import math
import warnings
from pathlib import Path

import yaml

from path_utils import BASELINE_DIR, directory_component, get_project_output_root, resolve_config_path

MODULES = ('main_effect', 'mediation_effect', 'moderation_effect', 'result_synthesis')
BLOCKS = dict(zip(MODULES[:3], ('hypotheses', 'mechanism_paths', 'moderation_paths')))
DIRECTIONS = {'positive', 'negative', 'any'}
FORMS = {'linear', 'u_shape', 'inverted_u_shape'}
BOOL_KEYS = set('enabled required early_stop fail_fast overwrite_existing '
                'entity_fixed_effect time_fixed_effect center_before_square create_squared_terms '
                'center_only_continuous_variables auto_create_terms expected_change_last '
                'effect_terms_long_format keep_wide_raw_results path_level_results chain_level_results '
                'strict_common_columns normalize_control_variable_text verbose '
                'x_weakened_after_m_required run_only_for_candidates minimum_sample_size_required '
                'draw_plot_in_current_module generate_dofile_in_this_version'.split())


class ConfigError(ValueError):
    pass


class ConfigWarning(UserWarning):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    pairs = loader.construct_pairs(node, deep=deep)
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f'YAML 重复键 {key!r}，第 {node.start_mark.line + 1} 行附近')
        result[key] = value
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_config_file(path):
    path = Path(path).resolve()
    with path.open(encoding='utf-8') as handle:
        cfg = yaml.load(handle, Loader=UniqueKeyLoader)
    if not isinstance(cfg, dict):
        raise ConfigError(f'{path}: 配置必须为 YAML 字典')
    cfg.update(_config_path=str(path), _config_dir=str(path.parent),
               _base_dir=str(path.parent.parent if path.parent.name == 'config' else path.parent))
    return cfg


def names(value):
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value):
        return value
    raise ConfigError(f'变量名单必须为空格分隔的文本或文本列表，收到 {value!r}')


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _enum(value, choices, field):
    _require(isinstance(value, str) and value in choices, f'{field} 必须为 {sorted(choices)}，收到 {value!r}')


def _number(value, field, low=0, high=None, integer=False):
    _require(type(value) in (int, float) and math.isfinite(value), f'{field} 必须是有限数值')
    _require(not integer or type(value) is int, f'{field} 必须是整数')
    _require(value >= low and (high is None or value <= high), f'{field} 超出允许范围')


def _walk(value, path=''):
    if isinstance(value, dict):
        for key, item in value.items():
            field = f'{path}.{key}' if path else key
            is_bool = (key in BOOL_KEYS or (path.endswith('treatment_profiles') and key in {'interpolate', 'winsorize'})
                       or key.startswith(('save_', 'check_', 'stop_if_', 'require_',
                       'include_', 'fail_on_', 'fail_if_', 'keep_all_', 'show_', 'add_source_', 'write_'))
                       or path.endswith('enabled_modules') or path.endswith('centering.apply_to'))
            if is_bool:
                _require(type(item) is bool, f'{field} 必须是 true/false，不能是 {item!r}')
            if key in {'p_value_threshold', 'strong_pass', 'weak_pass', 'confidence_level',
                       'strong_p_threshold', 'weak_p_threshold'} and not isinstance(item, str):
                _number(item, field, 0, 1)
                _require(0 < item < 1, f'{field} 必须介于 0 和 1 之间')
            _walk(item, field)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _walk(item, f'{path}[{i}]')


def _records(cfg, block, id_field='id'):
    rows = cfg.get(block, [])
    _require(isinstance(rows, list), f'{block} 必须为列表')
    seen = set()
    for row in rows:
        _require(isinstance(row, dict), f'{block} 中每项必须为字典')
        ident = row.get(id_field)
        _require(isinstance(ident, str) and bool(ident.strip()), f'{block}.{id_field} 不能为空')
        _require(ident not in seen, f'{block} 中编号重复: {ident}（包括停用示例）')
        seen.add(ident)
    return rows


def validate_global(cfg):
    _walk(cfg)
    _require(str(cfg.get('config_version')) == '2.0', '当前全局 config_version 仅支持 2.0')
    get_project_output_root(cfg)
    notes = []
    run = cfg.get('run_settings', {})
    _require(run.get('run_id') is None or isinstance(run['run_id'], str), 'run_id 必须是文本或 null')
    switches = run.get('enabled_modules', {})
    _require(not set(switches) - set(MODULES), 'enabled_modules 含未知模块')
    data = cfg.get('data', {})
    _require(isinstance(data.get('input_file'), str) and bool(data['input_file'].strip()), 'data.input_file 不能为空')
    _require(Path(data['input_file']).suffix.lower() in {'.dta', '.csv', '.xlsx'},
             'data.input_file 仅支持 .dta / .csv / .xlsx')
    if cfg.get('validation', {}).get('check_data_file_exists', True):
        resolve_config_path(data['input_file'], cfg, must_exist=True)
    info = data.get('sample_info', {})
    directory_component(info.get('sample_period', 'unknown_period'), 'data.sample_info.sample_period')
    rules = info.get('sample_rules', [])
    _require(isinstance(rules, list) and all(isinstance(x, str) for x in rules), 'sample_rules 必须为文本列表（仅说明，不执行筛选）')
    for rule in rules:
        directory_component(rule, 'data.sample_info.sample_rules')
    cont = cfg.get('continuous_variable_settings', {})
    continuous = set(names(cont.get('continuous_variables')))
    excluded = set(names(cont.get('exclude_variables')))
    for key in ('interpolate_allowed', 'winsorize_allowed'):
        permitted = set(names(cont.get(key)))
        _require(not permitted - continuous, f'{key} 必须是 continuous_variables 子集: {sorted(permitted - continuous)}')
        _require(not permitted & excluded, f'{key} 不得包含 exclude_variables: {sorted(permitted & excluded)}')
    controls = cfg.get('controls', {})
    required, optional = names(controls.get('required_controls')), names(controls.get('optional_controls'))
    for key, values in [('required_controls', required), ('optional_controls', optional)]:
        _require(len(values) == len(set(values)), f'controls.{key} 包含重复变量')
    _require(not set(required) & set(optional), '必选与可选控制变量不能重复')
    _require(set(names(controls.get('active_optional_controls'))) <= set(optional), 'active_optional_controls 必须来自 optional_controls')
    combo = controls.get('combination_rules', {})
    minimum, maximum = combo.get('min_optional_controls_in_combination', 0), combo.get('max_optional_controls_in_combination')
    _number(minimum, 'min_optional_controls_in_combination', integer=True)
    if maximum is not None:
        _number(maximum, 'max_optional_controls_in_combination', integer=True)
        _require(minimum <= maximum <= len(optional), '控制变量组合范围必须满足 min <= max <= 可选变量数')
    _require(minimum <= len(optional), '最小组合数超过可选变量数')
    search = cfg.get('combination_execution', {})
    _enum(search.get('mode', 'sampling_enumeration'), {'full_enumeration', 'sampling_enumeration'}, 'combination_execution.mode')
    for key, low in [('random_seed', 0), ('max_candidates_to_expand', 1), ('random_seeds_per_size', 1), ('local_expansions_per_seed', 0), ('top_k_limit', 1)]:
        if search.get(key) is not None:
            _number(search[key], f'combination_execution.{key}', low, integer=True)
    if search.get('local_expansions_per_seed', 0):
        raise ConfigError('local_expansions_per_seed 当前未实现，请设为 0')
    prep = cfg.get('preprocessing', {})
    thresholds = prep.get('sample_thresholds', [2, 3, 4, 5])
    _require(isinstance(thresholds, list) and bool(thresholds), 'sample_thresholds 必须为非空列表')
    for x in thresholds:
        _number(x, 'sample_thresholds', 1, integer=True)
    _require(len(set(thresholds)) == len(thresholds), 'sample_thresholds 不得重复')
    for profile, block in [('interpolate', 'interpolation'), ('winsorize', 'winsorize')]:
        settings = prep.get(block, {})
        selected = prep.get('treatment_profiles', {}).get(profile, False)
        if 'enabled' in settings:
            _require(settings['enabled'] == selected, f'preprocessing.{block}.enabled 已弃用且与 treatment_profiles.{profile} 冲突；请删除重复开关')
            notes.append(f'preprocessing.{block}.enabled 已弃用；由 treatment_profiles.{profile} 唯一控制方案展开')
    interp, winsor = prep.get('interpolation', {}), prep.get('winsorize', {})
    _enum(interp.get('method', 'linear'), {'linear'}, 'interpolation.method')
    _enum(interp.get('scope', 'within_entity'), {'within_entity'}, 'interpolation.scope')
    _enum(interp.get('limit_direction', 'both'), {'both', 'forward', 'backward'}, 'interpolation.limit_direction')
    if interp.get('max_gap') is not None:
        _number(interp['max_gap'], 'interpolation.max_gap', 1, integer=True)
    _enum(winsor.get('method', 'winsorize'), {'winsorize'}, 'winsorize.method')
    _enum(winsor.get('scope', 'full_sample'), {'full_sample', 'by_year', 'by_industry', 'by_industry_year'}, 'winsorize.scope')
    lower, upper = winsor.get('lower_quantile', .01), winsor.get('upper_quantile', .99)
    _number(lower, 'lower_quantile', 0, 1); _number(upper, 'upper_quantile', 0, 1)
    _require(lower < upper, '缩尾分位数必须满足 lower < upper')
    if winsor.get('scope') in {'by_industry', 'by_industry_year'}:
        _require(bool(winsor.get('industry_name')), '按行业缩尾必须指定 industry_name')
    model = cfg.get('model_settings', {})
    _number(model.get('minimum_sample_size', 1), 'minimum_sample_size', 1, integer=True)
    fe, panel = model.get('fixed_effects', {}), data.get('panel', {})
    for kind in ('entity', 'time'):
        if fe.get(f'{kind}_fixed_effect', False):
            _require(bool(fe.get(f'{kind}_name')), f'固定效应缺少 {kind}_name')
        if fe.get(f'{kind}_name') and panel.get(f'{kind}_name'):
            _require(fe[f'{kind}_name'] == panel[f'{kind}_name'], f'panel 与 fixed_effects 的 {kind}_name 不一致')
    cluster = model.get('clustered_standard_errors', {})
    _require(not cluster.get('enabled') or bool(cluster.get('cluster_level')), '启用聚类标准误时 cluster_level 不能为空')
    output = cfg.setdefault('output_settings', {})
    standardized = output.get('standardized_output', {})
    if standardized.get('enabled', True) and standardized.get('save_standardized_results', True):
        _require(standardized.get('include_formula', True),
                 '机器衔接结果必须保留模型公式；请将 standardized_output.include_formula 设为 true')
    if 'save_config_snapshot' in run:
        if 'save_config_snapshot' in output:
            _require(run['save_config_snapshot'] == output['save_config_snapshot'], '快照开关冲突：仅保留 output_settings.save_config_snapshot')
        else:
            output['save_config_snapshot'] = run['save_config_snapshot']
        notes.append('run_settings.save_config_snapshot 已弃用，请使用 output_settings.save_config_snapshot')
    return notes


def validate_module(global_cfg, cfg, module):
    _walk(cfg)
    _require(cfg.get('module_name') == module, f'module_name 应为 {module}')
    _require(str(cfg.get('config_version')) == ('2.1' if module == 'result_synthesis' else '2.0'), f'{module} config_version 不兼容')
    notes = []
    if module == 'result_synthesis':
        input_cfg = cfg.get('input', {})
        _enum(input_cfg.get('module_selection_mode', 'enabled_modules'), {'enabled_modules', 'listed_modules'}, 'input.module_selection_mode')
        selected_modules = input_cfg.get('modules', MODULES[:3])
        _require(isinstance(selected_modules, list) and all(isinstance(m, str) for m in selected_modules),
                 'input.modules 必须是模块名称列表')
        _require(set(selected_modules) <= set(MODULES[:3]), 'input.modules 包含未知模块')
        _require(len(selected_modules) == len(set(selected_modules)), 'input.modules 不能重复')
        registered = _records(cfg.get('hypothesis_registry', {}), 'expected_hypotheses')
        for row in registered:
            ident = row['id']
            owners = row.get('modules')
            _require(isinstance(owners, list) and bool(owners) and
                     all(isinstance(owner, str) and owner in MODULES[:3] for owner in owners),
                     f'{ident}: modules 必须列出至少一个有效效应模块')
            _require(len(owners) == len(set(owners)), f'{ident}: modules 不能重复')
            _enum(row.get('required_pass', 'none'), {'strong', 'weak', 'none'},
                  f'hypothesis_registry.expected_hypotheses.{ident}.required_pass')
        tables = cfg.get('pass_tables', {})
        _enum(tables.get('source_phase', 'C'), {'C'}, 'pass_tables.source_phase')
        _enum(tables.get('row_unit', 'specification_id'), {'specification_id'}, 'pass_tables.row_unit')
        _enum(tables.get('hypothesis_scope', 'selected_enabled_registered'),
              {'selected_enabled_registered'}, 'pass_tables.hypothesis_scope')
        basenames = []
        for name in ('require', 'full', 'strong', 'weak'):
            table = tables.get(name, {})
            _require(isinstance(table, dict) and table.get('enabled', True) is True,
                     f'pass_tables.{name}.enabled 必须为 true，以保持四表完整')
            basename = table.get('output_basename')
            _require(isinstance(basename, str), f'pass_tables.{name}.output_basename 不能为空')
            directory_component(basename, f'pass_tables.{name}.output_basename')
            basenames.append(basename)
        _require(len(basenames) == len(set(basenames)), 'pass_tables 的四个 output_basename 不能重复')
        _require(tables.get('full', {}).get('include_failed_models', True) is True,
                 'pass_tables.full.include_failed_models 必须为 true')
        for name in ('strong', 'weak'):
            _require(tables[name].get('require_all_hypotheses', True) is True,
                     f'pass_tables.{name}.require_all_hypotheses 必须为 true')
        _require(tables['weak'].get('include_strong_rows', True) is True,
                 'pass_tables.weak.include_strong_rows 必须为 true')
        _records(cfg.get('theory_chains', {}), 'chains', 'chain_id')
        output = cfg.get('output', {})
        directory_component(output.get('output_subdir', module), 'output.output_subdir')
        workbook = output.get('overview_workbook', {})
        _require(isinstance(workbook, dict), 'output.overview_workbook 必须是字典')
        if workbook.get('enabled', False):
            _require(output.get('save_excel', True) is True,
                     'output.overview_workbook.enabled=true 时 save_excel 也必须为 true')
            filename = workbook.get('filename')
            _require(isinstance(filename, str), 'output.overview_workbook.filename 不能为空')
            directory_component(filename, 'output.overview_workbook.filename')
            _require(filename.lower().endswith('.xlsx'), 'output.overview_workbook.filename 必须以 .xlsx 结尾')
            sheets = workbook.get('sheets')
            expected_sheets = ['require', 'full', 'strong', 'weak', '假设明细', '字段说明']
            _require(isinstance(sheets, list) and sheets == expected_sheets,
                     f'output.overview_workbook.sheets 必须依次为 {expected_sheets}')
        _enum(cfg.get('display_case_selection', {}).get('required_modules_mode', 'enabled_modules'), {'enabled_modules', 'theory_chain', 'custom'}, 'required_modules_mode')
        _enum(cfg.get('display_case_selection', {}).get('pass_level', 'strong'), {'strong', 'weak'}, 'pass_level')
        _enum(cfg.get('merge_settings', {}).get('missing_key_policy', 'warn_and_keep'), {'warn_and_keep', 'fail', 'drop'}, 'missing_key_policy')
        return notes
    rows = _records(cfg, BLOCKS[module])
    controls = set(names(global_cfg.get('controls', {}).get('required_controls'))) | set(names(global_cfg.get('controls', {}).get('optional_controls')))
    settings = cfg.get({'main_effect': 'main_effect_settings', 'mediation_effect': 'mediation_settings', 'moderation_effect': 'moderation_settings'}[module], {})
    for key, val in settings.get('combination_screening', {}).get('result_labels', {}).items():
        _number(val, f'{module}.{key}', 0, 1)
        _require(0 < val < 1, f'{key} 必须介于 0 和 1')
    labels = settings.get('combination_screening', {}).get('result_labels', {})
    _require(labels.get('strong_pass', .05) <= labels.get('weak_pass', .1), 'strong_pass 阈值不得大于 weak_pass')
    if module == 'mediation_effect':
        methods = cfg.get('screening_methods', {})
        for method, key in (('three_step', 'three_step_requirements'),
                            ('two_step', 'two_step_requirements')):
            defined = methods.get(method, {}).get('required_paths')
            if defined is None:
                continue
            _require(isinstance(defined, list) and set(defined) <= {'x_to_y', 'x_to_m', 'x_m_to_y'}
                     and len(defined) == len(set(defined)),
                     f'screening_methods.{method}.required_paths 包含未知或重复路径')
            flags = settings.get('candidate_rules', {}).get(key, {})
            expected = {name for flag, name in (('require_x_to_y', 'x_to_y'),
                        ('require_x_to_m', 'x_to_m'), ('require_m_to_y', 'x_m_to_y'))
                        if flags.get(flag, method == 'three_step' or flag != 'require_x_to_y')}
            _require(set(defined) == expected,
                     f'screening_methods.{method}.required_paths 与 candidate_rules.{key} 冲突')
    active = [r for r in rows if r.get('enabled', True)]
    if not active:
        _require(not global_cfg.get('validation', {}).get('stop_if_no_enabled_hypotheses', False), f'{module} 没有启用假设')
        notes.append(f'{module} 没有启用假设')
    for row in active:
        ident = row['id']
        if module == 'main_effect':
            _enum(row.get('model_form', 'linear'), FORMS, f'{ident}.model_form')
            _enum(row.get('pass_rule', 'all_required_terms'), {'all_required_terms', 'required_terms_only', 'any_required_term', 'shape_rule'}, f'{ident}.pass_rule')
            xs = row.get('x', [])
            _require(isinstance(xs, list) and bool(xs), f'{ident}.x 必须是非空列表')
            xnames = []
            for x in xs:
                _require(isinstance(x, dict) and bool(x.get('name')), f'{ident}.x 中每项必须含 name')
                xnames.append(x['name'])
                _enum(x.get('expected_direction', 'any'), DIRECTIONS, f'{ident}.expected_direction')
                if x.get('expected_shape'):
                    _enum(x['expected_shape'], {'u', 'inverted_u', 'u_shape', 'inverted_u_shape'}, f'{ident}.expected_shape')
            roles = xnames + [row.get('y')]
        elif module == 'mediation_effect':
            method = row.get('screening_method', cfg.get('screening_methods', {}).get('default_method', 'three_step'))
            _enum(method, {'three_step', 'two_step'}, f'{ident}.screening_method')
            _enum(row.get('pass_rule', 'all_required_paths'),
                  {'all_required_paths', 'core_mediation_paths'}, f'{ident}.pass_rule')
            _require(cfg.get('screening_methods', {}).get(method, {}).get('enabled', True), f'{ident} 引用了停用的 screening_method')
            mediators = names(row.get('mediators'))
            _require(bool(mediators), f'{ident}.mediators 不能为空')
            roles = [row.get('x'), row.get('y')] + mediators
            for key, value in row.get('path_directions', {}).items():
                _enum(value, DIRECTIONS, f'{ident}.path_directions.{key}')
            selected_paths = cfg.get('mediation_settings', {}).get('combination_screening', {}).get(
                'apply_to_paths', ['x_to_m', 'x_m_to_y'])
            _require(isinstance(selected_paths, list) and
                     set(selected_paths) <= {'x_to_y', 'x_to_m', 'x_m_to_y'} and
                     len(selected_paths) == len(set(selected_paths)),
                     'mediation_settings.combination_screening.apply_to_paths 包含未知或重复路径')
        else:
            _require(str(cfg.get('moderation_schema_version')) == '3.0', 'moderation_schema_version 必须为 3.0')
            relation, mod = row.get('focal_relationship', {}), row.get('moderation', {})
            form, modform = relation.get('form'), mod.get('form')
            _enum(form, FORMS, f'{ident}.focal_relationship.form')
            _enum(modform, {'linear', 'quadratic'}, f'{ident}.moderation.form')
            _require(form == 'linear' or modform == 'linear', f'{ident}: 暂不支持同时两种非线性')
            choices = (DIRECTIONS if modform == 'linear' else {'u_shape', 'inverted_u_shape'}) if form == 'linear' else {'steepen', 'flatten', 'turning_point_left', 'turning_point_right', 'any'}
            _enum(mod.get('expected_change'), choices, f'{ident}.expected_change')
            _enum(relation.get('direction', 'any'), DIRECTIONS, f'{ident}.direction')
            roles = [relation.get('from'), relation.get('to'), row.get('moderator')]
        _require(all(isinstance(v, str) and v.strip() for v in roles), f'{ident}: 变量名不能为空')
        _require(len(roles) == len(set(roles)), f'{ident}: 核心变量角色冲突或变量重复')
        _require(not set(roles) & controls, f'{ident}: 核心变量与控制变量重复 {sorted(set(roles) & controls)}')
        if module != 'moderation_effect' and global_cfg.get('validation', {}).get('check_hypotheses_variables_in_variable_blocks', True):
            blocks = cfg.get('variables', {})
            declarations = {'dependent_variables': [row['y']], 'independent_variables': xnames if module == 'main_effect' else [row['x']]}
            if module == 'mediation_effect':
                declarations['mediator_variables'] = mediators
            for block, values in declarations.items():
                declared = {v for group in blocks.get(block, {}).values() for v in names(group)}
                _require(set(values) <= declared, f'{ident}: {block} 未声明 {sorted(set(values) - declared)}')
    return notes


def validate_data_columns(cfg, module_cfg, columns):
    """Only active model variables are mandatory; inactive examples remain editable."""
    validation = cfg.get('validation', {})
    if not validation.get('check_variables_exist', True):
        return []
    required = set()
    if validation.get('check_global_variables_exist', True):
        for key in ('required_controls', 'optional_controls', 'active_optional_controls'):
            required.update(names(cfg.get('controls', {}).get(key)))
        for key in ('continuous_variables', 'interpolate_allowed', 'winsorize_allowed', 'exclude_variables'):
            required.update(names(cfg.get('continuous_variable_settings', {}).get(key)))
        required.update(v for v in cfg.get('data', {}).get('panel', {}).values() if isinstance(v, str) and v)
        fe = cfg.get('model_settings', {}).get('fixed_effects', {})
        required.update(fe.get(f'{kind}_name') for kind in ('entity', 'time') if fe.get(f'{kind}_fixed_effect'))
        cluster = cfg.get('model_settings', {}).get('clustered_standard_errors', {})
        if cluster.get('enabled'):
            required.add(cluster.get('cluster_level'))
        winsor = cfg.get('preprocessing', {}).get('winsorize', {})
        if winsor.get('scope') in {'by_industry', 'by_industry_year'}:
            required.add(winsor.get('industry_name'))
    module = module_cfg['module_name']
    if validation.get('check_module_variables_exist', True):
        for row in module_cfg.get(BLOCKS.get(module, ''), []):
            if not row.get('enabled', True):
                continue
            if module == 'main_effect':
                required.add(row['y']); required.update(x['name'] for x in row['x'])
            elif module == 'mediation_effect':
                required.update([row['x'], row['y']] + names(row['mediators']))
            else:
                required.update([row['focal_relationship']['from'], row['focal_relationship']['to'], row['moderator']])
                required.update(names(row.get('conditioning_variables')))
    missing = required - set(columns)
    if not missing:
        return []
    message = f'{module}: 数据缺少变量 {sorted(missing)}'
    _require(not validation.get('stop_if_missing_required_variables', True), message)
    return [message + '；已按配置仅提醒，后续模型仍可能失败']


def validate_references(global_cfg, synthesis, modules):
    notes = []
    registered = synthesis.get('hypothesis_registry', {}).get('expected_hypotheses', [])
    lookup = {(m, r['id']): r for m, cfg in modules.items() if m in BLOCKS for r in cfg.get(BLOCKS[m], [])}
    input_cfg = synthesis.get('input', {})
    selected = (set(input_cfg.get('modules', [])) if input_cfg.get('module_selection_mode') == 'listed_modules'
                else set(MODULES[:3]))
    enabled = {m for m in selected if global_cfg.get('run_settings', {}).get('enabled_modules', {}).get(m, False)
               and modules.get(m, {}).get('enabled', True)}
    for row in registered:
        for module in row.get('modules', []):
            _require(module in MODULES[:3], f"假设登记 {row['id']} 引用未知模块 {module}")
            match = lookup.get((module, row['id']))
            if match is None:
                notes.append(f"synthesis 登记 {module}/{row['id']} 在模块配置中不存在；未自动改写")
            elif row.get('description') != match.get('description'):
                notes.append(f"synthesis 登记 {module}/{row['id']} 的描述与模块配置不同；请核对研究设定")
            if row.get('required_pass', 'none') != 'none' and (
                    module not in enabled or match is None or not match.get('enabled', True)):
                notes.append(f"synthesis 登记 {module}/{row['id']} 设置了 required_pass，"
                             '但本轮未选中或未启用；不会纳入 require/strong/weak 判定')
    select = synthesis.get('display_case_selection', {})
    used = select.get('required_modules_mode') == 'theory_chain' or select.get('require_theory_chain', False)
    chains_cfg = synthesis.get('theory_chains', {})
    chains = [x for x in chains_cfg.get('chains', []) if x.get('enabled', True)] if chains_cfg.get('enabled', True) else []
    _require(not used or bool(chains), '展示案例要求理论链，但没有启用的理论链')
    for chain in chains:
        needed_modules = set(chain.get('required_modules', []))
        _require(bool(needed_modules) and needed_modules <= set(MODULES[:3]), f"{chain['chain_id']}: required_modules 无效")
        problems = []
        for ident in chain.get('required_hypotheses', []):
            found = [(m, r) for (m, key), r in lookup.items() if key == ident and m in needed_modules]
            if not found or not any(r.get('enabled', True) and global_cfg.get('run_settings', {}).get('enabled_modules', {}).get(m, False) and modules[m].get('enabled', True) for m, r in found):
                problems.append(ident)
        if problems:
            message = f"{chain['chain_id']}: 所需假设不存在或未启用 {problems}；未自动改写理论链"
            _require(not used, message)
            notes.append(message + '（当前未按理论链选案例，仅提醒）')
    return notes


def emit_notes(notes):
    for note in dict.fromkeys(notes):
        warnings.warn(note, ConfigWarning, stacklevel=2)


def preflight(global_cfg, module_cfg, columns=None):
    notes = validate_global(global_cfg)
    notes += validate_module(global_cfg, module_cfg, module_cfg['module_name'])
    if columns is not None:
        notes += validate_data_columns(global_cfg, module_cfg, columns)
    emit_notes(notes)


def preflight_synthesis(global_cfg, synthesis_cfg):
    """Check the complete configuration graph before reading any prior output."""
    notes = validate_global(global_cfg)
    notes += validate_module(global_cfg, synthesis_cfg, 'result_synthesis')
    modules = {'result_synthesis': synthesis_cfg}
    for module in MODULES[:3]:
        path = global_cfg.get('module_config_files', {}).get(module)
        _require(bool(path), f'module_config_files 缺少 {module}')
        modules[module] = load_config_file(resolve_config_path(path, global_cfg, must_exist=True))
        notes += validate_module(global_cfg, modules[module], module)
    notes += validate_references(global_cfg, synthesis_cfg, modules)
    emit_notes(notes)


def main():
    parser = argparse.ArgumentParser(description='只检查配置、数据列和输出位置，不回归、不写结果')
    parser.add_argument('config', nargs='?', default=str(BASELINE_DIR / 'config/analysis_config.yaml'))
    args = parser.parse_args()
    cfg = load_config_file(args.config)
    notes = validate_global(cfg)
    modules = {}
    for module in MODULES:
        path = cfg.get('module_config_files', {}).get(module)
        _require(bool(path), f'module_config_files 缺少 {module}')
        modules[module] = load_config_file(resolve_config_path(path, cfg, must_exist=True))
        notes += validate_module(cfg, modules[module], module)
    import pandas as pd
    data_path = resolve_config_path(cfg['data']['input_file'], cfg, must_exist=True)
    if data_path.suffix.lower() == '.dta':
        with pd.read_stata(data_path, iterator=True) as reader:
            columns = reader.read(1).columns
    elif data_path.suffix.lower() == '.csv':
        columns = pd.read_csv(data_path, nrows=0).columns
    elif data_path.suffix.lower() == '.xlsx':
        columns = pd.read_excel(data_path, nrows=0).columns
    else:
        raise ConfigError(f'不支持的数据格式 {data_path.suffix}')
    for module in MODULES[:3]:
        if cfg.get('run_settings', {}).get('enabled_modules', {}).get(module, False) and modules[module].get('enabled', True):
            notes += validate_data_columns(cfg, modules[module], columns)
    notes += validate_references(cfg, modules['result_synthesis'], modules)
    emit_notes(notes)
    print(f'配置校验通过；研究设定提醒 {len(set(notes))} 条。')
    print(f'项目输出根目录：{get_project_output_root(cfg)}')
    print('样本区间与剔除规则仅为说明；本命令未运行回归、未改写数据或结果。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
        raise SystemExit(f'配置校验失败：{exc}')
