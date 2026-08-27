-- q01
SELECT COUNT(*) FROM k8s_logs WHERE trace_id = '339354527adf581e8ece2eaea8cde703';
-- q02
SELECT * FROM k8s_logs WHERE trace_id = '339354527adf581e8ece2eaea8cde703' ORDER BY _timestamp DESC LIMIT 100;
-- q03
SELECT COUNT(*) FROM k8s_logs WHERE span_id = '7a4e90080fd2a013';
-- q04
SELECT * FROM k8s_logs WHERE span_id = '7a4e90080fd2a013' ORDER BY _timestamp DESC LIMIT 100;
-- q05
SELECT COUNT(*) FROM k8s_logs WHERE regexp_match(lower(message), '(^|[^a-z])474dbfe4c65a971176ae239869a6ba47([^a-z]|$)') IS NOT NULL;
-- q06
SELECT * FROM k8s_logs WHERE regexp_match(lower(message), '(^|[^a-z])474dbfe4c65a971176ae239869a6ba47([^a-z]|$)') IS NOT NULL ORDER BY _timestamp DESC LIMIT 100;
-- q07
SELECT COUNT(*) FROM k8s_logs WHERE regexp_match(lower(message), '(^|[^a-z])failed([^a-z]|$)') IS NOT NULL;
-- q08
SELECT * FROM k8s_logs WHERE regexp_match(lower(message), '(^|[^a-z])failed([^a-z]|$)') IS NOT NULL ORDER BY _timestamp DESC LIMIT 100;
-- q09
SELECT COUNT(*) FROM k8s_logs WHERE kubernetes_container_name = 'api-gateway-container' AND trace_id = '339354527adf581e8ece2eaea8cde703';
-- q10
SELECT * FROM k8s_logs WHERE kubernetes_container_name = 'api-gateway-container' AND trace_id = '339354527adf581e8ece2eaea8cde703' ORDER BY _timestamp DESC LIMIT 100;
-- q11
SELECT COUNT(*) FROM k8s_logs WHERE kubernetes_container_name = 'api-gateway-container' AND regexp_match(lower(message), '(^|[^a-z])474dbfe4c65a971176ae239869a6ba47([^a-z]|$)') IS NOT NULL;
-- q12
SELECT * FROM k8s_logs WHERE kubernetes_container_name = 'api-gateway-container' AND regexp_match(lower(message), '(^|[^a-z])474dbfe4c65a971176ae239869a6ba47([^a-z]|$)') IS NOT NULL ORDER BY _timestamp DESC LIMIT 100;
-- q13
SELECT COUNT(*) FROM k8s_logs WHERE regexp_match(lower(message), '(^|[^a-z])failed([^a-z]|$)') IS NOT NULL AND regexp_match(lower(message), '(^|[^a-z])474dbfe4c65a971176ae239869a6ba47([^a-z]|$)') IS NOT NULL;
-- q14
SELECT * FROM k8s_logs WHERE regexp_match(lower(message), '(^|[^a-z])failed([^a-z]|$)') IS NOT NULL AND regexp_match(lower(message), '(^|[^a-z])474dbfe4c65a971176ae239869a6ba47([^a-z]|$)') IS NOT NULL ORDER BY _timestamp DESC LIMIT 100;
-- q15
SELECT COUNT(*) FROM k8s_logs WHERE kubernetes_pod_name = 'api-gateway-9a2c1e6f4-8923f';
-- q16
SELECT * FROM k8s_logs WHERE kubernetes_pod_name = 'api-gateway-9a2c1e6f4-8923f' ORDER BY _timestamp DESC LIMIT 100;
-- q17
SELECT date_trunc('hour', to_timestamp_micros(_timestamp)) AS hour, COUNT(*) FROM k8s_logs GROUP BY hour ORDER BY hour;
-- q18
SELECT kubernetes_namespace_name, COUNT(*) AS c FROM k8s_logs GROUP BY kubernetes_namespace_name ORDER BY c DESC, kubernetes_namespace_name ASC LIMIT 10;
-- q19
SELECT date_trunc('hour', to_timestamp_micros(_timestamp)) AS hour, COUNT(*) FROM k8s_logs WHERE regexp_match(lower(message), '(^|[^a-z])failed([^a-z]|$)') IS NOT NULL GROUP BY hour ORDER BY hour;
