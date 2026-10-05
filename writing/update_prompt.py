import re

# Read v7 draft
with open('geo_agentscope/writing/ranking_prompt_v7_draft.py', 'r', encoding='utf-8') as f:
    v7_content = f.read()

# Extract the prompt string
match = re.search(r'RANKING_ARTICLE_PROMPT = """(.*?)"""', v7_content, re.DOTALL)
if match:
    new_prompt = 'RANKING_ARTICLE_PROMPT = """' + match.group(1) + '"""'
    print(f'Extracted v7 prompt: {len(new_prompt)} chars')
    
    # Read article_writer.py
    with open('geo_agentscope/writing/article_writer.py', 'r', encoding='utf-8') as f:
        writer_content = f.read()
    
    # Replace old prompt
    old_pattern = r'RANKING_ARTICLE_PROMPT = """.*?"""'
    new_content = re.sub(old_pattern, new_prompt, writer_content, flags=re.DOTALL)
    
    # Write back
    with open('geo_agentscope/writing/article_writer.py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print('Done! article_writer.py updated with v7 prompt.')
else:
    print('Error: Could not extract prompt from v7 draft')
