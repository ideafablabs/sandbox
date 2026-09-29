/***********************************************************************
SandboxMask - Vrui vislet that blacks out everything the sandbox
projector would draw outside the box. run-sandbox.sh loads it into
SARndbox:

  SARndbox ... -vislet SandboxMask etc/SARndbox-2.8 ;

The one argument is the SARndbox configuration directory (default
etc/SARndbox-2.8, relative to the working directory). From it the vislet
reads BoxLayout.txt (base plane and the four measured corners),
ProjectorMatrix.dat (the -fpv projector transformation) and EdgeMask.cfg,
which the Calibrate Sandbox wizard writes:

  enabled 1          # 0 switches the mask off without deleting the file
  left 1.5           # margin in cm outside the measured corner rectangle,
  right 1.5          # per edge: positive shows more, negative hides more.
  bottom 0           # Edges are named as the camera sees the corners
  top -0.5           # (see MaskGeometry.h); the wizard relabels them
  highlight left     # draw the outline and this edge in colour, honoured
                     # for 30 s after the file was written

The corners, grown by the margins, go through the projector matrix
exactly like SARndbox sends the sand surface through it, so the mask
follows every recalibration by itself. The three files are re-read
whenever they change, which is how the wizard's nudges show up at once.

How it draws: Vrui renders vislets before the application, so the mask
cannot simply be painted on top. It fills everything outside the box with
black at the near plane and writes that into the depth buffer; the sand
surface SARndbox draws afterwards fails the depth test there and stays
black. SARndbox renders with depth testing on and never clears the depth
buffer on its -fpv path, and CalibrateProjector maps the sand well inside
the depth range, so the surface is always behind the mask.

Console commands (on stdin, like Vrui's showMessage and quit):
  sandboxMaskInfo                  state, margins and on-screen corners
  sandboxMaskReload                re-read the three files now
  sandboxMaskSet <l> <r> <b> <t>   margins in cm, until a file changes
  sandboxMaskEnable on|off         until a file changes
  sandboxMaskHighlight <edge>|off  until a file changes
Everything it prints starts with "SandboxMask: ". Part of the Idea Fab
Labs sandbox install (see bin/CalibrateSandbox.py); it is not part of
Vrui or SARndbox and does not change them.
***********************************************************************/

#include <time.h>
#include <sys/stat.h>
#include <string>
#include <vector>
#include <iostream>
#include <sstream>
#include <GL/gl.h>
#include <GL/GLContextData.h>
#include <Misc/CommandDispatcher.h>
#include <Plugins/FactoryManager.h>
#include <Vrui/Vrui.h>
#include <Vrui/Vislet.h>
#include <Vrui/VisletManager.h>

#include "MaskGeometry.h"

namespace {

const char* PREFIX="SandboxMask: ";
const char* FILE_NAMES[3]={"BoxLayout.txt","ProjectorMatrix.dat","EdgeMask.cfg"};
const double CHECK_INTERVAL=0.5; // Seconds between looks at the files
const double HIGHLIGHT_SECONDS=30.0; // How long a highlight taken from the file stays visible
const double FAR=20.0; // How far past the screen the black polygons reach (the screen is 2 units wide)

struct FileStamp
	{
	bool exists;
	long long size;
	long long mtimeNs;
	
	FileStamp(void)
		:exists(false),size(0),mtimeNs(0)
		{
		}
	static FileStamp of(const std::string& fileName)
		{
		FileStamp result;
		struct stat st;
		if(stat(fileName.c_str(),&st)==0)
			{
			result.exists=true;
			result.size=st.st_size;
			result.mtimeNs=(long long)st.st_mtim.tv_sec*1000000000LL+st.st_mtim.tv_nsec;
			}
		return result;
		}
	bool operator!=(const FileStamp& other) const
		{
		return exists!=other.exists||size!=other.size||mtimeNs!=other.mtimeNs;
		}
	};

std::string trimmed(const char* begin,const char* end)
	{
	while(begin<end&&isspace(*begin))
		++begin;
	while(end>begin&&isspace(end[-1]))
		--end;
	return std::string(begin,end);
	}

}

class SandboxMask;

class SandboxMaskFactory:public Vrui::VisletFactory
	{
	friend class SandboxMask;
	
	public:
	SandboxMaskFactory(Vrui::VisletManager& visletManager);
	virtual ~SandboxMaskFactory(void);
	virtual Vrui::Vislet* createVislet(int numVisletArguments,const char* const visletArguments[]) const;
	virtual void destroyVislet(Vrui::Vislet* vislet) const;
	};

class SandboxMask:public Vrui::Vislet
	{
	friend class SandboxMaskFactory;
	
	private:
	static SandboxMaskFactory* factory;
	std::string configDir;
	FileStamp stamps[3];
	double lastCheck; // Application time of the last look at the files
	MaskGeometry::BoxLayout layout;
	bool layoutValid;
	double projector[16];
	bool projectorValid;
	MaskGeometry::MaskConfig config; // Margins, enabled flag and highlight in force
	bool configValid; // EdgeMask.cfg exists and could be read
	bool geometryValid; // screenCorners hold the mask outline
	double screenCorners[4][2]; // Mask corners in normalized device coordinates, BoxLayout.txt order
	time_t highlightUntil; // Wall-clock end of the highlight taken from the file; 0 = no expiry
	std::string problem; // Why the mask is off, when it is
	
	std::string path(int fileIndex) const;
	bool filesChanged(void);
	void reload(void);
	void computeGeometry(void);
	bool maskOn(void) const;
	void printState(void) const;
	static void infoCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	static void reloadCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	static void setCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	static void enableCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	static void highlightCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData);
	
	public:
	SandboxMask(int numArguments,const char* const arguments[]);
	virtual ~SandboxMask(void);
	virtual Vrui::VisletFactory* getFactory(void) const;
	virtual void frame(void);
	virtual void display(GLContextData& contextData) const;
	};

/***********************************
Methods of class SandboxMaskFactory:
***********************************/

SandboxMaskFactory::SandboxMaskFactory(Vrui::VisletManager& visletManager)
	:Vrui::VisletFactory("SandboxMask",visletManager)
	{
	SandboxMask::factory=this;
	}

SandboxMaskFactory::~SandboxMaskFactory(void)
	{
	SandboxMask::factory=0;
	}

Vrui::Vislet* SandboxMaskFactory::createVislet(int numVisletArguments,const char* const visletArguments[]) const
	{
	return new SandboxMask(numVisletArguments,visletArguments);
	}

void SandboxMaskFactory::destroyVislet(Vrui::Vislet* vislet) const
	{
	delete vislet;
	}

extern "C" void resolveSandboxMaskDependencies(Plugins::FactoryManager<Vrui::VisletFactory>& manager)
	{
	}

extern "C" Vrui::VisletFactory* createSandboxMaskFactory(Plugins::FactoryManager<Vrui::VisletFactory>& manager)
	{
	Vrui::VisletManager* visletManager=static_cast<Vrui::VisletManager*>(&manager);
	return new SandboxMaskFactory(*visletManager);
	}

extern "C" void destroySandboxMaskFactory(Vrui::VisletFactory* factory)
	{
	delete factory;
	}

/************************************
Static elements of class SandboxMask:
************************************/

SandboxMaskFactory* SandboxMask::factory=0;

/****************************
Methods of class SandboxMask:
****************************/

SandboxMask::SandboxMask(int numArguments,const char* const arguments[])
	:configDir(numArguments>=1&&arguments[0][0]!='\0'?arguments[0]:"etc/SARndbox-2.8"),
	 lastCheck(0.0),layoutValid(false),projectorValid(false),configValid(false),geometryValid(false),
	 highlightUntil(0)
	{
	Misc::CommandDispatcher& dispatcher=Vrui::getCommandDispatcher();
	dispatcher.addCommandCallback("sandboxMaskInfo",&SandboxMask::infoCommandCallback,this,0,"Prints the edge mask state and its on-screen corners");
	dispatcher.addCommandCallback("sandboxMaskReload",&SandboxMask::reloadCommandCallback,this,0,"Re-reads BoxLayout.txt, ProjectorMatrix.dat and EdgeMask.cfg");
	dispatcher.addCommandCallback("sandboxMaskSet",&SandboxMask::setCommandCallback,this,"<left> <right> <bottom> <top>","Sets the edge margins in cm until a file changes");
	dispatcher.addCommandCallback("sandboxMaskEnable",&SandboxMask::enableCommandCallback,this,"on|off","Switches the edge mask until a file changes");
	dispatcher.addCommandCallback("sandboxMaskHighlight",&SandboxMask::highlightCommandCallback,this,"left|right|bottom|top|off","Draws the mask outline with one edge in colour until a file changes");
	std::cout<<PREFIX<<"loaded, reading "<<configDir<<std::endl;
	filesChanged();
	reload();
	}

SandboxMask::~SandboxMask(void)
	{
	/* The command callbacks stay registered: vislets are only destroyed while Vrui shuts down. */
	}

Vrui::VisletFactory* SandboxMask::getFactory(void) const
	{
	return factory;
	}

std::string SandboxMask::path(int fileIndex) const
	{
	return configDir+"/"+FILE_NAMES[fileIndex];
	}

bool SandboxMask::filesChanged(void)
	{
	bool changed=false;
	for(int i=0;i<3;++i)
		{
		FileStamp now=FileStamp::of(path(i));
		if(now!=stamps[i])
			changed=true;
		stamps[i]=now;
		}
	return changed;
	}

void SandboxMask::reload(void)
	{
	std::string error;
	problem.clear();
	
	layoutValid=MaskGeometry::readBoxLayout(path(0),layout,error);
	if(!layoutValid)
		problem=error;
	
	projectorValid=MaskGeometry::readProjectorMatrix(path(1),projector,error);
	if(!projectorValid&&problem.empty())
		problem=error;
	
	MaskGeometry::MaskConfig fileConfig;
	configValid=MaskGeometry::readMaskConfig(path(2),fileConfig,error);
	if(configValid)
		{
		config=fileConfig;
		highlightUntil=config.highlight>=0?time_t(stamps[2].mtimeNs/1000000000LL)+time_t(HIGHLIGHT_SECONDS):0;
		}
	else
		{
		config=MaskGeometry::MaskConfig();
		config.enabled=false;
		if(problem.empty())
			problem=stamps[2].exists?error:"no EdgeMask.cfg in "+configDir+" yet";
		}
	
	computeGeometry();
	printState();
	}

void SandboxMask::computeGeometry(void)
	{
	geometryValid=false;
	if(!layoutValid||!projectorValid)
		return;
	double corners[4][3];
	std::string error;
	if(!MaskGeometry::maskCorners(layout,config.margins,corners,error))
		{
		if(problem.empty())
			problem=error;
		return;
		}
	for(int i=0;i<4;++i)
		{
		double ndc[3];
		if(!MaskGeometry::projectPoint(projector,corners[i],ndc))
			{
			if(problem.empty())
				problem="a mask corner lies behind the projector";
			return;
			}
		screenCorners[i][0]=ndc[0];
		screenCorners[i][1]=ndc[1];
		}
	geometryValid=true;
	}

bool SandboxMask::maskOn(void) const
	{
	return geometryValid&&config.enabled;
	}

void SandboxMask::printState(void) const
	{
	std::ostringstream s;
	s<<PREFIX;
	if(maskOn())
		s<<"on";
	else
		s<<"off";
	if(!problem.empty())
		s<<" ("<<problem<<")";
	s<<", margins";
	for(int e=0;e<MaskGeometry::NUM_EDGES;++e)
		s<<" "<<MaskGeometry::edgeName(e)<<" "<<config.margins[e];
	s<<" cm, highlight "<<MaskGeometry::edgeName(config.highlight);
	if(geometryValid)
		{
		static const char* cornerNames[4]={"LL","LR","UL","UR"};
		s<<", screen corners";
		for(int i=0;i<4;++i)
			s<<" "<<cornerNames[i]<<" ("<<screenCorners[i][0]<<", "<<screenCorners[i][1]<<")";
		}
	std::cout<<s.str()<<std::endl;
	}

void SandboxMask::infoCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	static_cast<SandboxMask*>(userData)->printState();
	}

void SandboxMask::reloadCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	SandboxMask* thisPtr=static_cast<SandboxMask*>(userData);
	thisPtr->filesChanged();
	thisPtr->reload();
	Vrui::requestUpdate();
	}

void SandboxMask::setCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	SandboxMask* thisPtr=static_cast<SandboxMask*>(userData);
	std::istringstream in(trimmed(argumentBegin,argumentEnd));
	double margins[MaskGeometry::NUM_EDGES];
	for(int e=0;e<MaskGeometry::NUM_EDGES;++e)
		if(!(in>>margins[e]))
			{
			std::cout<<PREFIX<<"error: sandboxMaskSet needs four numbers (left right bottom top, in cm)"<<std::endl;
			return;
			}
	for(int e=0;e<MaskGeometry::NUM_EDGES;++e)
		thisPtr->config.margins[e]=margins[e];
	thisPtr->problem.clear();
	thisPtr->computeGeometry();
	thisPtr->printState();
	Vrui::requestUpdate();
	}

void SandboxMask::enableCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	SandboxMask* thisPtr=static_cast<SandboxMask*>(userData);
	std::string arg=trimmed(argumentBegin,argumentEnd);
	thisPtr->config.enabled=!(arg=="off"||arg=="0"||arg=="false");
	thisPtr->printState();
	Vrui::requestUpdate();
	}

void SandboxMask::highlightCommandCallback(const char* argumentBegin,const char* argumentEnd,void* userData)
	{
	SandboxMask* thisPtr=static_cast<SandboxMask*>(userData);
	thisPtr->config.highlight=MaskGeometry::edgeIndex(trimmed(argumentBegin,argumentEnd));
	thisPtr->highlightUntil=0;
	thisPtr->printState();
	Vrui::requestUpdate();
	}

void SandboxMask::frame(void)
	{
	double now=Vrui::getApplicationTime();
	if(now-lastCheck>=CHECK_INTERVAL)
		{
		lastCheck=now;
		if(filesChanged())
			reload();
		}
	Vrui::scheduleUpdate(now+CHECK_INTERVAL);
	}

void SandboxMask::display(GLContextData& contextData) const
	{
	if(!maskOn())
		return;
	
	/* Plain fixed-function drawing straight in clip coordinates, on top of whatever Vrui set up: */
	glPushAttrib(GL_ENABLE_BIT|GL_DEPTH_BUFFER_BIT|GL_COLOR_BUFFER_BIT|GL_CURRENT_BIT|GL_LINE_BIT|GL_POLYGON_BIT|GL_LIGHTING_BIT);
	glDisable(GL_LIGHTING);
	glDisable(GL_TEXTURE_2D);
	glDisable(GL_BLEND);
	glDisable(GL_CULL_FACE);
	glDisable(GL_ALPHA_TEST);
	glDisable(GL_FOG);
	for(int i=0;i<6;++i)
		glDisable(GL_CLIP_PLANE0+i);
	glEnable(GL_DEPTH_TEST);
	glDepthFunc(GL_ALWAYS);
	glDepthMask(GL_TRUE);
	glPolygonMode(GL_FRONT_AND_BACK,GL_FILL);
	glMatrixMode(GL_PROJECTION);
	glPushMatrix();
	glLoadIdentity();
	glMatrixMode(GL_MODELVIEW);
	glPushMatrix();
	glLoadIdentity();
	
	double cx=0.0,cy=0.0;
	for(int i=0;i<4;++i)
		{
		cx+=screenCorners[i][0]*0.25;
		cy+=screenCorners[i][1]*0.25;
		}
	
	/* Everything outside a convex quadrilateral is the union of the outer half-planes of its edges;
	   each one is drawn as a long strip on the near plane (z=-1) with depth writes on: */
	glColor3f(0.0f,0.0f,0.0f);
	glBegin(GL_QUADS);
	for(int e=0;e<MaskGeometry::NUM_EDGES;++e)
		{
		int a,b;
		MaskGeometry::edgeCorners(e,a,b);
		double ax=screenCorners[a][0],ay=screenCorners[a][1];
		double bx=screenCorners[b][0],by=screenCorners[b][1];
		double dx=bx-ax,dy=by-ay;
		double len=sqrt(dx*dx+dy*dy);
		if(len<1.0e-9)
			continue;
		dx/=len;
		dy/=len;
		double nx=dy,ny=-dx;
		if(nx*((ax+bx)*0.5-cx)+ny*((ay+by)*0.5-cy)<0.0)
			{
			nx=-nx;
			ny=-ny;
			}
		double ex=dx*FAR,ey=dy*FAR,ox=nx*FAR,oy=ny*FAR;
		glVertex3d(ax-ex,ay-ey,-1.0);
		glVertex3d(bx+ex,by+ey,-1.0);
		glVertex3d(bx+ex+ox,by+ey+oy,-1.0);
		glVertex3d(ax-ex+ox,ay-ey+oy,-1.0);
		}
	glEnd();
	
	if(config.highlight>=0&&(highlightUntil==0||time(0)<highlightUntil))
		{
		/* The whole outline thin and white, the edge being adjusted thick and yellow: */
		static const int loop[4]={0,1,3,2};
		glLineWidth(2.0f);
		glColor3f(1.0f,1.0f,1.0f);
		glBegin(GL_LINE_LOOP);
		for(int i=0;i<4;++i)
			glVertex3d(screenCorners[loop[i]][0],screenCorners[loop[i]][1],-1.0);
		glEnd();
		int a,b;
		MaskGeometry::edgeCorners(config.highlight,a,b);
		glLineWidth(5.0f);
		glColor3f(1.0f,0.85f,0.0f);
		glBegin(GL_LINES);
		glVertex3d(screenCorners[a][0],screenCorners[a][1],-1.0);
		glVertex3d(screenCorners[b][0],screenCorners[b][1],-1.0);
		glEnd();
		}
	
	glPopMatrix();
	glMatrixMode(GL_PROJECTION);
	glPopMatrix();
	glMatrixMode(GL_MODELVIEW);
	glPopAttrib();
	}
