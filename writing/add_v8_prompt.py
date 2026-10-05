import re

# Read v8 template
with open('geo_agentscope/writing/ranking_list_prompt_v8.py', 'r', encoding='utf-8') as f:
    v8_content = f.read()

# Read article_writer.py
with open('geo_agentscope/writing/article_writer.py', 'r', encoding='utf-8') as f:
    writer_content = f.read()

# Find the end of RANKING_ARTICLE_PROMPT
match = re.search(r'(RANKING_ARTICLE_PROMPT = """.*?""")', writer_content, re.DOTALL)
if match:
    insert_pos = match.end()
    
    # Extract just the RANKING_LIST_PROMPT part
    v8_match = re.search(r'(RANKING_LIST_PROMPT = """.*?""")', v8_content, re.DOTALL)
    if v8_match:
        new_prompt = v8_match.group(0)
        
        # Insert after RANKING_ARTICLE_PROMPT
        new_content = writer_content[:insert_pos] + '\n\n\n' + new_prompt + writer_content[insert_pos:]
        
        # Write back
        with open('geo_agentscope/writing/article_writer.py', 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        print('Done! Added RANKING_LIST_PROMPT to article_writer.py')
    else:
        print('Error: Could not find RANKING_LIST_PROMPT in v8 file')
else:
    print('Error: Could not find RANKING_ARTICLE_PROMPT in article_writer.py')
