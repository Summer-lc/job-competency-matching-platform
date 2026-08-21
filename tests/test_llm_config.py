def test_llm_module_imports_as_src_package():
    from src.llm import MODEL_REGISTRY, get_llm

    assert callable(get_llm)
    assert set(MODEL_REGISTRY) == {"deepseek-v4-pro", "deepseek-v4-flash"}
