def read_from_file(filename):
    with open(filename, 'r') as file:
        return file.read().strip()

def write_to_file(filename, content):
    with open(filename, 'w') as file:
        file.write(content)

