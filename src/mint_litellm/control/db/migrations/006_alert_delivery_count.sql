-- 전송 행이 만들어질 때의 경보 횟수. 횟수가 바뀌면 같은 경보라도 새 전송을 만든다.
ALTER TABLE mint.alert_deliveries
    ADD COLUMN alert_count integer NOT NULL DEFAULT 1 CHECK (alert_count >= 1);

DROP TRIGGER alert_deliveries_2_frozen ON mint.alert_deliveries;
CREATE TRIGGER alert_deliveries_2_frozen BEFORE UPDATE ON mint.alert_deliveries
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'alert_id', 'channel', 'delivery_id', 'alert_count');
