
def format_memory(kb_memory):
    if kb_memory >= 1024:
        mb_memory = kb_memory / 1024
        return f'{mb_memory:.2f} MB'
    else:
        return f'{kb_memory} KB'

