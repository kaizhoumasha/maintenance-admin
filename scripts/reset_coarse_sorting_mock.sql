BEGIN;

SET LOCAL search_path TO enterprise_backend;

-- 仅清理粗分机 RS01/RS02 产生的过程数据，保留工作线、设备、绑定和 MOCK 配置。
CREATE TEMP TABLE _coarse_cmd ON COMMIT DROP AS
SELECT command_code
FROM ecs_command
WHERE owner_domain = 'coarse_sorting'
   OR device_code LIKE 'RS-%';

CREATE TEMP TABLE _coarse_receipt ON COMMIT DROP AS
SELECT receipt_id
FROM callback_receipt
WHERE owner_domain = 'coarse_sorting'
   OR device_code LIKE 'RS-%';

DELETE FROM ecs_result_evidence
WHERE command_code IN (SELECT command_code FROM _coarse_cmd);

DELETE FROM ecs_send_evidence
WHERE command_code IN (SELECT command_code FROM _coarse_cmd);

DELETE FROM ecs_command
WHERE command_code IN (SELECT command_code FROM _coarse_cmd);

DELETE FROM callback_receipt
WHERE receipt_id IN (SELECT receipt_id FROM _coarse_receipt);

-- 先把业务记录置为可删除的初始状态，满足现有 CHECK 约束。
UPDATE coarse_sorting_job
SET state = 'AWAITING_VALIDATION',
    current_action_id = NULL,
    allocation_action_id = NULL,
    rack_preparation_id = NULL,
    placed_at_ms = NULL,
    completed_at = NULL
WHERE workline_code IN ('RS01', 'RS02')
   OR work_line_id IN (
       SELECT id
       FROM work_lines
       WHERE line_code IN ('CLN-01', 'CLN-02')
   );

DELETE FROM coarse_sorting_action
WHERE job_id IN (
          SELECT id
          FROM coarse_sorting_job
          WHERE workline_code IN ('RS01', 'RS02')
             OR work_line_id IN (
                 SELECT id FROM work_lines WHERE line_code IN ('CLN-01', 'CLN-02')
             )
      )
   OR preparation_id IN (
          SELECT id
          FROM coarse_sorting_rack_preparation
          WHERE workline_code IN ('RS01', 'RS02')
             OR work_line_id IN (
                 SELECT id FROM work_lines WHERE line_code IN ('CLN-01', 'CLN-02')
             )
      )
   OR input_preparation_id IN (
          SELECT id
          FROM coarse_sorting_rack_preparation
          WHERE workline_code IN ('RS01', 'RS02')
             OR work_line_id IN (
                 SELECT id FROM work_lines WHERE line_code IN ('CLN-01', 'CLN-02')
             )
      )
   OR result_preparation_id IN (
          SELECT id
          FROM coarse_sorting_rack_preparation
          WHERE workline_code IN ('RS01', 'RS02')
             OR work_line_id IN (
                 SELECT id FROM work_lines WHERE line_code IN ('CLN-01', 'CLN-02')
             )
      );

DELETE FROM coarse_sorting_rack_preparation
WHERE workline_code IN ('RS01', 'RS02')
   OR work_line_id IN (
       SELECT id FROM work_lines WHERE line_code IN ('CLN-01', 'CLN-02')
   );

DELETE FROM coarse_sorting_job
WHERE workline_code IN ('RS01', 'RS02')
   OR work_line_id IN (
       SELECT id FROM work_lines WHERE line_code IN ('CLN-01', 'CLN-02')
   );

-- 清空两轮 MOCK 的预约和回复状态，保留 round_id 与配置摘要。
UPDATE coarse_sorting_mock_round
SET state = '{}'::jsonb
WHERE round_id IN (
    'rs01-mock-20261120T213210Z',
    'rs02-mock-20261120T213210Z'
);

COMMIT;

-- 可选核验
SELECT 'jobs' AS item, count(*) AS remaining
FROM coarse_sorting_job
WHERE workline_code IN ('RS01', 'RS02')
UNION ALL
SELECT 'actions', count(*)
FROM coarse_sorting_action
WHERE device_code LIKE 'RS-%'
UNION ALL
SELECT 'rack_preparations', count(*)
FROM coarse_sorting_rack_preparation
WHERE workline_code IN ('RS01', 'RS02')
UNION ALL
SELECT 'ecs_commands', count(*)
FROM ecs_command
WHERE owner_domain = 'coarse_sorting'
   OR device_code LIKE 'RS-%'
UNION ALL
SELECT 'callback_receipts', count(*)
FROM callback_receipt
WHERE owner_domain = 'coarse_sorting'
   OR device_code LIKE 'RS-%';
