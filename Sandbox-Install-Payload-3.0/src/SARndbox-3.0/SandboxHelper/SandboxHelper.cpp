/***********************************************************************
SandboxHelper - Vrui vislet that the Calibrate Sandbox wizard loads into
RawKinectViewer ("RawKinectViewer -vislet SandboxHelper ;"). It adds
console commands, read from stdin like Vrui's own showMessage and quit,
so the wizard can drive the UC Davis tools without modifying them:

  sandboxAverage on|off   Presses the "Average Frames" main menu entry.
                          "on" re-captures if the entry is already on and
                          prints "SandboxHelper: average frame ready" once
                          the capture dialog has closed again.
  sandboxMessage <text>   Shows <text> in the plugin's own popup, replacing
                          the previous one (no stacking).
  sandboxCloseMessages    Closes the plugin's popup.
  sandboxWatch on|off     Reports RawKinectViewer's capture dialog ("capture
                          started" / "capture done", one pair per "Save Plane"
                          press of the Calibrate Depth Lens tool) and every new
                          Vrui error popup ("popup <title>: <text>"), which is
                          the only way that tool reports a failure.

The plugin never touches Vrui's own message dialogs (showMessage): Vrui
keeps those in a heap and deletes them itself after one minute, and
hiding or deleting them from a console command breaks that cleanup. The
wizard therefore uses sandboxMessage for everything once the plugin has
reported "SandboxHelper: loaded".

Everything it prints starts with "SandboxHelper: ". Part of the Idea Fab
Labs sandbox install (see bin/CalibrateSandbox.py); it is not part of
Vrui, Kinect or SARndbox and does not change them.
***********************************************************************/

#include <string.h>
#include <string>
#include <vector>
#include <iostream>
#include <Misc/CallbackData.h>
#include <Misc/CallbackList.h>
#include <Misc/CommandDispatcher.h>
#include <Plugins/FactoryManager.h>
#include <GLMotif/Widget.h>
#include <GLMotif/Container.h>
#include <GLMotif/SingleChildContainer.h>
#include <GLMotif/RowColumn.h>
#include <GLMotif/Popup.h>
#include <GLMotif/PopupMenu.h>
#include <GLMotif/PopupWindow.h>
#include <GLMotif/Button.h>
#include <GLMotif/ToggleButton.h>
#include <GLMotif/Label.h>
#include <GLMotif/Margin.h>
#include <GLMotif/WidgetManager.h>
#include <Vrui/Vrui.h>
#include <Vrui/MutexMenu.h>
#include <Vrui/Vislet.h>
#include <Vrui/VisletManager.h>

namespace {

/* Widget names from RawKinectViewer.cpp (createMainMenu, createAverageDepthFrameDialog)
   and Vrui's showErrorMessage: */
const char* AVERAGE_TOGGLE_NAME="AverageFramesButton";
const char* AVERAGE_DIALOG_NAME="AverageDepthFrameDialogPopup";
const char* ERROR_POPUP_NAME="VruiErrorMessage"; // Vrui::showErrorMessage / showMessage dialogs
const char* PREFIX="SandboxHelper: ";
const size_t LINE_LENGTH=40; // Same wrapping width Vrui uses for its message dialogs

std::vector<std::string> wrapLines(const std::string& text)
	{
	std::vector<std::string> lines;
	std::string line,word;
	for(std::string::const_iterator cIt=text.begin();;++cIt)
		{
		if(cIt==text.end()||isspace(*cIt))
			{
			if(!word.empty())
				{
				if(!line.empty()&&line.length()+1+word.length()>LINE_LENGTH)
					{
					lines.push_back(line);
					line.clear();
					}
				if(!line.empty())
					line.push_back(' ');
				line+=word;
				word.clear();
				}
			if(cIt==text.end())
				break;
			}
		else
			word.push_back(*cIt);
		}
	if(!line.empty())
		lines.push_back(line);
	return lines;
	}

/* Collect the text of all labels inside a widget tree, skipping buttons (which are labels too): */
void collectText(GLMotif::Widget* widget,std::string& text)
	{
	if(dynamic_cast<GLMotif::Button*>(widget)!=0)
		return;
	GLMotif::Label* label=dynamic_cast<GLMotif::Label*>(widget);
	if(label!=0)
		{
		if(!text.empty())
			text.push_back(' ');
		text.append(label->getString());
		return;
		}
	GLMotif::Container* container=dynamic_cast<GLMotif::Container*>(widget);
	if(container!=0)
		for(GLMotif::Widget* child=container->getFirstChild();child!=0;child=container->getNextChild(child))
			collectText(child,text);
	}

std::string trimmed(const char* begin,const char* end)
	{
	while(begin<end&&isspace(*begin))
		++begin;
	while(end>begin&&isspace(end[-1]))
		--end;
	return std::string(begin,end);
	}

}

class SandboxHelper;

class SandboxHelperFactory:public Vrui::VisletFactory
	{
	friend class SandboxHelper;
	
	public:
	SandboxHelperFactory(Vrui::VisletManager& visletManager);
	virtual ~SandboxHelperFactory(void);
	virtual Vrui::Vislet* createVislet(int numVisletArguments,const char* const visletArguments[]) const;
	virtual void destroyVislet(Vrui::Vislet* vislet) const;
	};

class SandboxHelper:public Vrui::Vislet
	{
	friend class SandboxHelperFactory;
	
	private:
	static SandboxHelperFactory* factory;
	bool watchingAverage; // True while waiting for the average frame capture dialog to close
	bool sawAverageDialog; // The capture dialog has been visible since the toggle was pressed
	double watchStart; // Application time when the watch began
	GLMotif::PopupWindow* messageDialog; // The plugin's own message popup, or 0
	bool watching; // sandboxWatch on: report the capture dialog and error popups
	bool captureVisible; // Last reported state of the capture dialog
	std::vector<GLMotif::Widget*> reportedPopups; // Error popups already reported while watching
	
	static void averageCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	static void messageCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	static void closeMessagesCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	static void watchCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	GLMotif::ToggleButton* findAverageToggle(void) const;
	static void pressToggle(GLMotif::ToggleButton* toggle,bool set);
	void showMessage(const std::string& text);
	int closeMessages(void);
	void okCallback(Misc::CallbackData* cbData);
	
	public:
	SandboxHelper(int numArguments,const char* const arguments[]);
	virtual ~SandboxHelper(void);
	virtual Vrui::VisletFactory* getFactory(void) const;
	virtual void disable(bool shutdown);
	virtual void frame(void);
	};

/*************************************
Methods of class SandboxHelperFactory:
*************************************/

SandboxHelperFactory::SandboxHelperFactory(Vrui::VisletManager& visletManager)
	:Vrui::VisletFactory("SandboxHelper",visletManager)
	{
	SandboxHelper::factory=this;
	}

SandboxHelperFactory::~SandboxHelperFactory(void)
	{
	SandboxHelper::factory=0;
	}

Vrui::Vislet* SandboxHelperFactory::createVislet(int numVisletArguments,const char* const visletArguments[]) const
	{
	return new SandboxHelper(numVisletArguments,visletArguments);
	}

void SandboxHelperFactory::destroyVislet(Vrui::Vislet* vislet) const
	{
	delete vislet;
	}

extern "C" void resolveSandboxHelperDependencies(Plugins::FactoryManager<Vrui::VisletFactory>& manager)
	{
	}

extern "C" Vrui::VisletFactory* createSandboxHelperFactory(Plugins::FactoryManager<Vrui::VisletFactory>& manager)
	{
	Vrui::VisletManager* visletManager=static_cast<Vrui::VisletManager*>(&manager);
	return new SandboxHelperFactory(*visletManager);
	}

extern "C" void destroySandboxHelperFactory(Vrui::VisletFactory* factory)
	{
	delete factory;
	}

/**************************************
Static elements of class SandboxHelper:
**************************************/

SandboxHelperFactory* SandboxHelper::factory=0;

/******************************
Methods of class SandboxHelper:
******************************/

SandboxHelper::SandboxHelper(int numArguments,const char* const arguments[])
	:watchingAverage(false),sawAverageDialog(false),watchStart(0.0),messageDialog(0),
	 watching(false),captureVisible(false)
	{
	Misc::CommandDispatcher& dispatcher=Vrui::getCommandDispatcher();
	dispatcher.addCommandCallback("sandboxAverage",&SandboxHelper::averageCommandCallback,this,"on|off","Presses RawKinectViewer's Average Frames menu entry");
	dispatcher.addCommandCallback("sandboxMessage",&SandboxHelper::messageCommandCallback,this,"<message text>","Replaces open message popups with a new one");
	dispatcher.addCommandCallback("sandboxCloseMessages",&SandboxHelper::closeMessagesCommandCallback,this,0,"Closes all open message popups");
	dispatcher.addCommandCallback("sandboxWatch",&SandboxHelper::watchCommandCallback,this,"on|off","Reports the average frame capture dialog and Vrui error popups");
	std::cout<<PREFIX<<"loaded"<<std::endl;
	}

SandboxHelper::~SandboxHelper(void)
	{
	/* The command callbacks stay registered: vislets are only destroyed while Vrui shuts down. */
	}

void SandboxHelper::disable(bool shutdown)
	{
	if(shutdown)
		closeMessages(); // While the widget manager still exists
	Vrui::Vislet::disable(shutdown);
	}

Vrui::VisletFactory* SandboxHelper::getFactory(void) const
	{
	return factory;
	}

GLMotif::ToggleButton* SandboxHelper::findAverageToggle(void) const
	{
	Vrui::MutexMenu* mainMenu=Vrui::getMainMenu();
	if(mainMenu==0)
		return 0;
	GLMotif::PopupMenu* popup=mainMenu->getPopup();
	if(popup==0)
		return 0;
	GLMotif::RowColumn* menu=popup->getMenu();
	if(menu==0)
		return 0;
	return dynamic_cast<GLMotif::ToggleButton*>(menu->findChild(AVERAGE_TOGGLE_NAME));
	}

void SandboxHelper::pressToggle(GLMotif::ToggleButton* toggle,bool set)
	{
	/* Do what a menu click does: flip the state, then run the value changed callbacks: */
	toggle->setToggle(set);
	GLMotif::ToggleButton::ValueChangedCallbackData cbData(toggle,set);
	toggle->getValueChangedCallbacks().call(&cbData);
	}

void SandboxHelper::showMessage(const std::string& text)
	{
	/* Replace the previous popup. Deleting it here is safe: this runs from a console command,
	   outside widget event processing and outside the popup's own callbacks. */
	closeMessages();
	
	messageDialog=new GLMotif::PopupWindow("SandboxHelperMessage",Vrui::getWidgetManager(),"Calibrate Sandbox");
	messageDialog->setResizableFlags(false,false);
	messageDialog->setHideButton(false);
	
	GLMotif::RowColumn* body=new GLMotif::RowColumn("Body",messageDialog,false);
	body->setOrientation(GLMotif::RowColumn::VERTICAL);
	body->setPacking(GLMotif::RowColumn::PACK_TIGHT);
	
	std::vector<std::string> lines=wrapLines(text);
	for(std::vector<std::string>::const_iterator lIt=lines.begin();lIt!=lines.end();++lIt)
		new GLMotif::Label("Line",body,lIt->c_str());
	
	GLMotif::Margin* buttonMargin=new GLMotif::Margin("ButtonMargin",body,false);
	buttonMargin->setAlignment(GLMotif::Alignment::RIGHT);
	GLMotif::Button* okButton=new GLMotif::Button("OkButton",buttonMargin,"OK");
	okButton->getSelectCallbacks().add(this,&SandboxHelper::okCallback);
	buttonMargin->manageChild();
	body->manageChild();
	
	Vrui::popupPrimaryWidget(messageDialog);
	}

int SandboxHelper::closeMessages(void)
	{
	if(messageDialog==0)
		return 0;
	delete messageDialog; // Pops itself down in its destructor
	messageDialog=0;
	return 1;
	}

void SandboxHelper::okCallback(Misc::CallbackData* cbData)
	{
	/* Called from a click, i.e. inside widget event processing: the manager defers the deletion
	   until the event is done, which is what Vrui's own message dialogs rely on too. */
	if(messageDialog!=0)
		{
		Vrui::getWidgetManager()->deleteWidget(messageDialog);
		messageDialog=0;
		}
	}

void SandboxHelper::averageCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	SandboxHelper* thisPtr=static_cast<SandboxHelper*>(userData);
	std::string arg=trimmed(argumentBegin,argumentEnd);
	bool on=!(arg=="off"||arg=="0"||arg=="false");
	
	GLMotif::ToggleButton* toggle=thisPtr->findAverageToggle();
	if(toggle==0)
		{
		std::cout<<PREFIX<<"error: Average Frames menu entry not found"<<std::endl;
		return;
		}
	if(on)
		{
		/* Switch off first so an existing average is thrown away and captured again: */
		if(toggle->getToggle())
			pressToggle(toggle,false);
		pressToggle(toggle,true);
		thisPtr->watchingAverage=true;
		thisPtr->sawAverageDialog=false;
		thisPtr->watchStart=Vrui::getApplicationTime();
		Vrui::requestUpdate();
		std::cout<<PREFIX<<"Average Frames on, capturing"<<std::endl;
		}
	else
		{
		if(toggle->getToggle())
			pressToggle(toggle,false);
		thisPtr->watchingAverage=false;
		std::cout<<PREFIX<<"Average Frames off"<<std::endl;
		}
	}

void SandboxHelper::messageCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	SandboxHelper* thisPtr=static_cast<SandboxHelper*>(userData);
	std::string text=trimmed(argumentBegin,argumentEnd);
	if(text.empty())
		thisPtr->closeMessages();
	else
		thisPtr->showMessage(text);
	}

void SandboxHelper::closeMessagesCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	SandboxHelper* thisPtr=static_cast<SandboxHelper*>(userData);
	int numClosed=thisPtr->closeMessages();
	std::cout<<PREFIX<<"closed "<<numClosed<<" message popup"<<(numClosed==1?"":"s")<<std::endl;
	}

void SandboxHelper::watchCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	SandboxHelper* thisPtr=static_cast<SandboxHelper*>(userData);
	std::string arg=trimmed(argumentBegin,argumentEnd);
	thisPtr->watching=!(arg=="off"||arg=="0"||arg=="false");
	thisPtr->captureVisible=false;
	thisPtr->reportedPopups.clear();
	Vrui::requestUpdate();
	std::cout<<PREFIX<<(thisPtr->watching?"watching":"not watching")<<std::endl;
	}

void SandboxHelper::frame(void)
	{
	if(!watchingAverage&&!watching)
		return;
	
	/* RawKinectViewer shows a "Capturing average depth frame..." dialog while it averages and pops it down when done;
	   Vrui's error popups (the only way the Calibrate Depth Lens tool reports a failure) are named VruiErrorMessage: */
	bool dialogVisible=false;
	std::vector<GLMotif::Widget*> errorPopups;
	GLMotif::WidgetManager* wm=Vrui::getWidgetManager();
	for(GLMotif::WidgetManager::PoppedWidgetIterator it=wm->beginPrimaryWidgets();it!=wm->endPrimaryWidgets();++it)
		if(it.isVisible())
			{
			if(strcmp((*it)->getName(),AVERAGE_DIALOG_NAME)==0)
				dialogVisible=true;
			else if(strcmp((*it)->getName(),ERROR_POPUP_NAME)==0)
				errorPopups.push_back(*it);
			}
	
	if(watchingAverage)
		{
		if(dialogVisible)
			sawAverageDialog=true;
		else if(sawAverageDialog)
			{
			watchingAverage=false;
			std::cout<<PREFIX<<"average frame ready"<<std::endl;
			}
		else if(Vrui::getApplicationTime()-watchStart>3.0)
			{
			/* The dialog never showed up (an average frame was already valid or this is not RawKinectViewer): */
			watchingAverage=false;
			std::cout<<PREFIX<<"average frame ready (no capture dialog seen)"<<std::endl;
			}
		}
	
	if(watching)
		{
		if(dialogVisible!=captureVisible)
			{
			captureVisible=dialogVisible;
			std::cout<<PREFIX<<(dialogVisible?"capture started":"capture done")<<std::endl;
			}
		for(std::vector<GLMotif::Widget*>::iterator pIt=errorPopups.begin();pIt!=errorPopups.end();++pIt)
			{
			bool reported=false;
			for(std::vector<GLMotif::Widget*>::iterator rIt=reportedPopups.begin();rIt!=reportedPopups.end()&&!reported;++rIt)
				reported=*rIt==*pIt;
			if(!reported)
				{
				/* Only ever read the popup; Vrui deletes it itself after a minute. */
				GLMotif::PopupWindow* popup=dynamic_cast<GLMotif::PopupWindow*>(*pIt);
				std::string text;
				collectText(*pIt,text);
				std::cout<<PREFIX<<"popup "<<(popup!=0?popup->getTitleString():"")<<": "<<text<<std::endl;
				}
			}
		reportedPopups=errorPopups;
		}
	
	if(watchingAverage||watching)
		Vrui::scheduleUpdate(Vrui::getApplicationTime()+0.1);
	}
