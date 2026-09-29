/***********************************************************************
MaskGeometry - Plain C++ helpers for the SandboxMask vislet: read
BoxLayout.txt, ProjectorMatrix.dat and EdgeMask.cfg, grow the measured
box rectangle by a margin per edge, and project the result the way
SARndbox -fpv projects the sand surface. No Vrui dependencies, so a
standalone program (and the Python copy in bin/CalibrateSandbox.py) can
check the numbers.

Edges are named after the corners in BoxLayout.txt, which the wizard
records as lower-left, lower-right, upper-left, upper-right as the camera
sees them: left = LL-UL, right = LR-UR, bottom = LL-LR, top = UL-UR. The
wizard shows them under the names of where they land on the screen.

Part of the Idea Fab Labs sandbox install; not part of Vrui or SARndbox.
***********************************************************************/

#ifndef MASKGEOMETRY_INCLUDED
#define MASKGEOMETRY_INCLUDED

#include <math.h>
#include <stdint.h>
#include <string.h>
#include <string>
#include <vector>
#include <fstream>
#include <sstream>

namespace MaskGeometry {

enum Edge
	{
	LEFT=0,RIGHT=1,BOTTOM=2,TOP=3,NUM_EDGES=4
	};

inline const char* edgeName(int edge)
	{
	static const char* names[NUM_EDGES]={"left","right","bottom","top"};
	return edge>=0&&edge<NUM_EDGES?names[edge]:"off";
	}

inline int edgeIndex(const std::string& name)
	{
	for(int i=0;i<NUM_EDGES;++i)
		if(name==edgeName(i))
			return i;
	return -1;
	}

/* The two corners (BoxLayout.txt order) at the ends of an edge: */
inline void edgeCorners(int edge,int& a,int& b)
	{
	static const int corners[NUM_EDGES][2]={{0,2},{1,3},{0,1},{2,3}};
	a=corners[edge][0];
	b=corners[edge][1];
	}

struct BoxLayout
	{
	double normal[3]; // Base plane normal, plane equation normal . x = offset
	double offset;
	double corners[4][3]; // Lower-left, lower-right, upper-left, upper-right in camera space (cm)
	};

struct MaskConfig
	{
	bool enabled;
	double margins[NUM_EDGES]; // cm outside the measured corners; negative moves the edge inwards
	int highlight; // Edge to draw in colour, or -1
	
	MaskConfig(void)
		:enabled(true),highlight(-1)
		{
		for(int i=0;i<NUM_EDGES;++i)
			margins[i]=0.0;
		}
	};

inline double dot3(const double* a,const double* b)
	{
	return a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
	}

inline void cross3(const double* a,const double* b,double* out)
	{
	out[0]=a[1]*b[2]-a[2]*b[1];
	out[1]=a[2]*b[0]-a[0]*b[2];
	out[2]=a[0]*b[1]-a[1]*b[0];
	}

inline double normalize3(double* v)
	{
	double m=sqrt(dot3(v,v));
	if(m>0.0)
		for(int i=0;i<3;++i)
			v[i]/=m;
	return m;
	}

inline std::string trim(const std::string& s)
	{
	size_t b=0,e=s.size();
	while(b<e&&isspace((unsigned char)s[b]))
		++b;
	while(e>b&&isspace((unsigned char)s[e-1]))
		--e;
	return s.substr(b,e-b);
	}

/* All numbers on a line, brackets and commas ignored: */
inline std::vector<double> numbersIn(const std::string& text)
	{
	std::string cleaned(text);
	for(size_t i=0;i<cleaned.size();++i)
		if(cleaned[i]=='('||cleaned[i]==')'||cleaned[i]==','||cleaned[i]=='\t')
			cleaned[i]=' ';
	std::istringstream in(cleaned);
	std::vector<double> result;
	double v;
	while(in>>v)
		result.push_back(v);
	return result;
	}

/* BoxLayout.txt: one plane line "(nx, ny, nz), offset" then four corner lines "(x, y, z)": */
inline bool readBoxLayout(const std::string& fileName,BoxLayout& layout,std::string& error)
	{
	std::ifstream in(fileName.c_str());
	if(!in)
		{
		error="cannot open "+fileName;
		return false;
		}
	BoxLayout result;
	int lineIndex=0;
	std::string line;
	while(lineIndex<5&&std::getline(in,line))
		{
		std::string t=trim(line);
		if(t.empty()||t[0]=='#')
			continue;
		std::vector<double> v=numbersIn(t);
		if(lineIndex==0)
			{
			if(v.size()!=4)
				{
				error=fileName+": the plane line needs 4 numbers";
				return false;
				}
			for(int i=0;i<3;++i)
				result.normal[i]=v[i];
			result.offset=v[3];
			}
		else
			{
			if(v.size()!=3)
				{
				error=fileName+": each corner line needs 3 numbers";
				return false;
				}
			for(int i=0;i<3;++i)
				result.corners[lineIndex-1][i]=v[i];
			}
		++lineIndex;
		}
	if(lineIndex<5)
		{
		error=fileName+": expected a plane and four corners";
		return false;
		}
	if(dot3(result.normal,result.normal)<=0.0)
		{
		error=fileName+": the plane normal is zero";
		return false;
		}
	layout=result;
	return true;
	}

/* ProjectorMatrix.dat: 16 little-endian doubles, row-major, as CalibrateProjector writes them: */
inline bool readProjectorMatrix(const std::string& fileName,double matrix[16],std::string& error)
	{
	std::ifstream in(fileName.c_str(),std::ios::binary);
	if(!in)
		{
		error="cannot open "+fileName;
		return false;
		}
	unsigned char bytes[16*8];
	in.read(reinterpret_cast<char*>(bytes),sizeof(bytes));
	if(in.gcount()!=static_cast<std::streamsize>(sizeof(bytes)))
		{
		error=fileName+": expected 128 bytes (16 doubles)";
		return false;
		}
	for(int i=0;i<16;++i)
		{
		uint64_t bits=0;
		for(int b=7;b>=0;--b)
			bits=(bits<<8)|bytes[i*8+b];
		double d;
		memcpy(&d,&bits,sizeof(d));
		if(d!=d||fabs(d)>1.0e300)
			{
			error=fileName+": contains a value that is not a number";
			return false;
			}
		matrix[i]=d;
		}
	return true;
	}

/* EdgeMask.cfg: "key value" lines, # comments. Keys: enabled, left, right, bottom, top, highlight. */
inline bool readMaskConfig(const std::string& fileName,MaskConfig& config,std::string& error)
	{
	std::ifstream in(fileName.c_str());
	if(!in)
		{
		error="cannot open "+fileName;
		return false;
		}
	MaskConfig result;
	std::string line;
	while(std::getline(in,line))
		{
		size_t hash=line.find('#');
		if(hash!=std::string::npos)
			line.erase(hash);
		std::istringstream ls(line);
		std::string key;
		if(!(ls>>key))
			continue;
		std::string value;
		std::getline(ls,value);
		value=trim(value);
		int edge=edgeIndex(key);
		if(key=="enabled")
			result.enabled=!(value=="0"||value=="off"||value=="false"||value=="no");
		else if(edge>=0)
			{
			std::istringstream vs(value);
			double m;
			if(!(vs>>m))
				{
				error=fileName+": "+key+" needs a number, not '"+value+"'";
				return false;
				}
			result.margins[edge]=m;
			}
		else if(key=="highlight")
			result.highlight=edgeIndex(value);
		/* Unknown keys are ignored so newer files still load. */
		}
	config=result;
	return true;
	}

inline void projectOntoPlane(const BoxLayout& layout,const double* p,double* out)
	{
	double d=(dot3(layout.normal,p)-layout.offset)/dot3(layout.normal,layout.normal);
	for(int i=0;i<3;++i)
		out[i]=p[i]-layout.normal[i]*d;
	}

struct Line2
	{
	double p[2];
	double d[2];
	};

inline bool intersect(const Line2& l1,const Line2& l2,double* out)
	{
	double den=l1.d[0]*l2.d[1]-l1.d[1]*l2.d[0];
	if(fabs(den)<1.0e-12)
		return false;
	double dx=l2.p[0]-l1.p[0];
	double dy=l2.p[1]-l1.p[1];
	double t=(dx*l2.d[1]-dy*l2.d[0])/den;
	out[0]=l1.p[0]+l1.d[0]*t;
	out[1]=l1.p[1]+l1.d[1]*t;
	return true;
	}

/* The measured corners, dropped onto the base plane like SARndbox does, then each edge
   moved outwards by its margin; result in BoxLayout.txt corner order. */
inline bool maskCorners(const BoxLayout& layout,const double* margins,double out[4][3],std::string& error)
	{
	double n[3]={layout.normal[0],layout.normal[1],layout.normal[2]};
	normalize3(n);
	double c[4][3];
	for(int i=0;i<4;++i)
		projectOntoPlane(layout,layout.corners[i],c[i]);
	
	/* In-plane axes: x along the lower and upper edges (as SARndbox builds its box frame), y = n x x: */
	double x[3],y[3],center[3];
	for(int i=0;i<3;++i)
		{
		x[i]=(c[1][i]-c[0][i])+(c[3][i]-c[2][i]);
		center[i]=(c[0][i]+c[1][i]+c[2][i]+c[3][i])*0.25;
		}
	double xn=dot3(x,n);
	for(int i=0;i<3;++i)
		x[i]-=n[i]*xn;
	if(normalize3(x)<1.0e-6)
		{
		error="the corners do not span a rectangle";
		return false;
		}
	cross3(n,x,y);
	normalize3(y);
	
	double uv[4][2];
	for(int i=0;i<4;++i)
		{
		double r[3]={c[i][0]-center[0],c[i][1]-center[1],c[i][2]-center[2]};
		uv[i][0]=dot3(r,x);
		uv[i][1]=dot3(r,y);
		}
	
	/* Each edge as a line, pushed away from the centre (the origin) by its margin: */
	Line2 lines[NUM_EDGES];
	for(int e=0;e<NUM_EDGES;++e)
		{
		int a,b;
		edgeCorners(e,a,b);
		double dx=uv[b][0]-uv[a][0];
		double dy=uv[b][1]-uv[a][1];
		double len=sqrt(dx*dx+dy*dy);
		if(len<1.0e-9)
			{
			error=std::string("the two corners of the ")+edgeName(e)+" edge coincide";
			return false;
			}
		dx/=len;
		dy/=len;
		double nx=dy,ny=-dx;
		double mx=(uv[a][0]+uv[b][0])*0.5;
		double my=(uv[a][1]+uv[b][1])*0.5;
		if(nx*mx+ny*my<0.0)
			{
			nx=-nx;
			ny=-ny;
			}
		lines[e].p[0]=uv[a][0]+nx*margins[e];
		lines[e].p[1]=uv[a][1]+ny*margins[e];
		lines[e].d[0]=dx;
		lines[e].d[1]=dy;
		}
	
	/* New corners where the moved edges meet: LL = left/bottom, LR = right/bottom, UL = left/top, UR = right/top: */
	static const int cornerEdges[4][2]={{LEFT,BOTTOM},{RIGHT,BOTTOM},{LEFT,TOP},{RIGHT,TOP}};
	for(int i=0;i<4;++i)
		{
		double p[2];
		if(!intersect(lines[cornerEdges[i][0]],lines[cornerEdges[i][1]],p))
			{
			error=std::string("the ")+edgeName(cornerEdges[i][0])+" and "+edgeName(cornerEdges[i][1])+" edges are parallel";
			return false;
			}
		for(int j=0;j<3;++j)
			out[i][j]=center[j]+x[j]*p[0]+y[j]*p[1];
		}
	return true;
	}

/* Camera-space point -> normalized device coordinates through the projector matrix (row-major),
   as SARndbox -fpv does for every sand vertex. False when the point is behind the projector. */
inline bool projectPoint(const double matrix[16],const double* p,double* ndc)
	{
	double clip[4];
	for(int i=0;i<4;++i)
		clip[i]=matrix[i*4+0]*p[0]+matrix[i*4+1]*p[1]+matrix[i*4+2]*p[2]+matrix[i*4+3];
	if(clip[3]<=1.0e-12)
		return false;
	for(int i=0;i<3;++i)
		ndc[i]=clip[i]/clip[3];
	return true;
	}

}

#endif
