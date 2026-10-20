"""Schema compatibility check.
Run it from the backend folder:   python check_schema_compat.py
It reads database/PostgreSQL Database schema.sql and every SQL statement in the Python code, and reports:
  1. tables or columns the code uses that the schema does not have
  2. UPDATE statements that write a value the schema does not allow
  3. UPDATE statements that change a column the database makes read only
  4. code that changes the table structure by itself
It only reads files. It never connects to a database."""
import ast, os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else '.'
SCHEMA = os.path.join(ROOT, 'database', 'PostgreSQL Database schema.sql')
SKIP = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'tests', 'baseline', 'runs', 'notebooks', 'scripts'}

schema = re.sub(r'--.*', '', open(SCHEMA, encoding='utf-8-sig').read().replace('\r', ''))
bodies = {m.group(1): m.group(2) for m in re.finditer(r'CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);', schema, re.S)}
columns = {t: set(re.findall(r'^\s{4}(?!CHECK|UNIQUE|PRIMARY|FOREIGN|CONSTRAINT)(\w+)\s+[A-Za-z]', b, re.M)) for t, b in bodies.items()}
for m in re.finditer(r'ALTER TABLE (?:ONLY )?(\w+)\s+ADD COLUMN (?:IF NOT EXISTS )?(\w+)', schema):
    columns.setdefault(m.group(1), set()).add(m.group(2))
allowed = {}   # (table, column) -> allowed values, from CHECK (column IN (...))
for t, b in bodies.items():
    for col, vals in re.findall(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)", b):
        allowed[(t, col)] = set(re.findall(r"'([^']*)'", vals))
fn = re.search(r'FUNCTION protect_field_predictions\(\).*?\$\$ LANGUAGE plpgsql;', schema, re.S)
read_only = set(re.findall(r'NEW\.(\w+) IS DISTINCT FROM OLD\.\1', fn.group(0))) if fn else set()

def statements(path):
    try: tree = ast.parse(open(path, encoding='utf-8-sig', errors='replace').read().replace('\r', ''))
    except SyntaxError: return
    for n in ast.walk(tree):
        text = None
        if isinstance(n, ast.Constant) and isinstance(n.value, str): text = n.value
        elif isinstance(n, ast.JoinedStr): text = ''.join(v.value if isinstance(v, ast.Constant) else ' {dynamic} ' for v in n.values)
        if text and len(text) > 25 and re.search(r'(?is)\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)|SELECT\b.+\bFROM\b)', text):
            yield n.lineno, re.sub(r'\s+', ' ', text).strip()

count, touched, missing, bad_values, protected, structure = 0, set(), [], [], [], []
for dp, dn, files in os.walk(ROOT):
    if set(dp.replace('\\', '/').split('/')) & SKIP: continue
    for f in files:
        if not f.endswith('.py'): continue
        rel = os.path.relpath(os.path.join(dp, f), ROOT)
        for line, q in statements(os.path.join(dp, f)):
            count += 1
            if re.match(r'(?i)(ALTER TABLE|CREATE (UNIQUE )?(TABLE|INDEX))', q):
                tbl = re.search(r'(?i)(?:TABLE|ON)\s+(?:IF NOT EXISTS\s+)?(\w+)', q)
                col = re.search(r'(?i)ADD COLUMN (?:IF NOT EXISTS )?(\w+)', q)
                done = tbl and col and col.group(1) in columns.get(tbl.group(1), set())
                structure.append((rel, line, q[:90] + ('  (the column already exists in the schema)' if done else '')))
                continue
            names = {}
            for m in re.finditer(r'(?i)\b(?:FROM|JOIN|UPDATE|INTO)\s+([a-z_][a-z0-9_]*)(?:\s+(?:AS\s+)?([a-z_][a-z0-9_]*))?', q):
                t, a = m.group(1), m.group(2)
                if t in columns:
                    touched.add(t); names[t] = t
                    if a and a.upper() not in {'SET', 'WHERE', 'ON', 'JOIN', 'LEFT', 'RIGHT', 'INNER', 'VALUES', 'USING', 'ORDER', 'GROUP', 'LIMIT', 'FOR', 'RETURNING', 'CROSS', 'FULL', 'AND', 'OR', 'SELECT'}: names[a] = t
            m = re.match(r'(?i)INSERT INTO (\w+)\s*\(([^)]*)\)', q)
            if m and m.group(1) in columns:
                missing += [(rel, line, f'INSERT INTO {m.group(1)}: no column {c!r}') for c in (x.strip() for x in m.group(2).split(',')) if c and c not in columns[m.group(1)]]
            m = re.match(r'(?i)UPDATE (\w+)(?:\s+(?:AS\s+)?\w+)?\s+SET (.*?)(?:\s+(?:FROM|WHERE|RETURNING)\b|$)', q)
            if m and m.group(1) in columns:
                t, setpart = m.group(1), m.group(2)
                for col in re.findall(r'(?:^|,)\s*(\w+)\s*=', setpart):
                    if col not in columns[t] and col != 'dynamic': missing.append((rel, line, f'UPDATE {t}: no column {col!r}'))
                    if t == 'field_predictions' and col in read_only: protected.append((rel, line, f'UPDATE field_predictions changes read only column {col!r}'))
                for col, val in re.findall(r"(?:^|,)\s*(\w+)\s*=\s*'([^']*)'", setpart):
                    if (t, col) in allowed and val not in allowed[(t, col)]:
                        bad_values.append((rel, line, f"UPDATE {t}: {col} = '{val}' is not allowed (allowed: {sorted(allowed[(t, col)])})"))
            for a, c in set(re.findall(r'\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b', q)):
                t = names.get(a)
                if t and c not in columns[t]: missing.append((rel, line, f'{a}.{c}: table {t} has no column {c!r}'))

def show(title, items):
    print(f'{title}: {len(set(items))}')
    for it in sorted(set(items)): print(f'   {it[0]} line {it[1]}: {it[2]}')
print(f'Schema: {len(bodies)} tables | code read: {count} SQL statements | schema tables used by the code: {len(touched)}')
print('Read only columns of field_predictions (from the database trigger):', ', '.join(sorted(read_only)) or 'none found')
print()
show('1. Tables or columns the code uses that the schema does not have', missing)
show('2. Values the code writes that the schema does not allow', bad_values)
show('3. Code that changes a read only column', protected)
show('4. Code that changes the table structure by itself (informational)', structure)
print('\nRESULT:', 'no problems found' if not (missing or bad_values or protected) else f'{len(set(missing)) + len(set(bad_values)) + len(set(protected))} problem(s) found')
