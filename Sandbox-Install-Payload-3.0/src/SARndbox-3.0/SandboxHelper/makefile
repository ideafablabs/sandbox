########################################################################
# Builds the SandboxHelper Vrui vislet against the installed Vrui and
# installs it into Vrui's vislet plug-in directory. Run by
# sandbox-install-3.0.sh; by hand:
#   make && sudo make install
# VRUI_MAKEINCLUDE is the application makefile fragment Vrui installs.
########################################################################

VRUI_MAKEINCLUDE ?= /usr/local/share/Vrui-8.0/Vrui.makeinclude
include $(VRUI_MAKEINCLUDE)

PLUGIN = libSandboxHelper.$(VRUI_PLUGINFILEEXT)

all: $(PLUGIN)

$(PLUGIN): SandboxHelper.cpp
	$(CXX) -O2 $(VRUI_CFLAGS) $(VRUI_PLUGINCFLAGS) -o $@ $< $(VRUI_PLUGINLINKFLAGS) $(VRUI_LINKFLAGS)

install: $(PLUGIN)
	install -d $(VRUI_VISLETSDIR)
	install -m 644 $(PLUGIN) $(VRUI_VISLETSDIR)/$(PLUGIN)

clean:
	rm -f $(PLUGIN)

.PHONY: all install clean
