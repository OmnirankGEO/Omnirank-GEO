from services.selected_keyword_resolver import resolve_selected_keyword_texts, safe_json_list


def test_resolves_session_local_ids_from_keywords_snapshot():
    snapshot = [
        {"id": 1, "keyword": "揭阳电梯维修"},
        {"id": 2, "keyword": "揭阳别墅电梯"},
        {"id": 3, "keyword": "揭阳家用电梯"},
    ]

    assert resolve_selected_keyword_texts([1, 3], keywords_snapshot=snapshot) == [
        "揭阳电梯维修",
        "揭阳家用电梯",
    ]


def test_resolves_cluster_promoted_keyword_ids_from_clusters_data():
    clusters_data = {
        "clusters": [
            {
                "core_keywords": [{"id": 10, "keyword": "汽车之家投放"}],
                "covered_keywords": [{"id": 11, "keyword": "懂车帝投放"}],
            }
        ],
        "unclustered_keywords": [{"id": 12, "keyword": "太平洋汽车投放"}],
    }

    assert resolve_selected_keyword_texts([11, 12], clusters_data=clusters_data) == [
        "懂车帝投放",
        "太平洋汽车投放",
    ]


def test_safe_json_list_rejects_non_list_values():
    assert safe_json_list("[1, 2, 3]") == [1, 2, 3]
    assert safe_json_list('{"id": 1}') == []
    assert safe_json_list("") == []
