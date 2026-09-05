import pathlib
new = open("generic_new.txt", "r", encoding="utf-8").read()
pathlib.Path("custom_components/telegraf_mqtt/parsers/generic.py").write_text(new)
print("Done")
